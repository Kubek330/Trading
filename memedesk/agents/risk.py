"""Risk: approves every ticket with its stop set before entry, then manages
every open position in both books with the same exits:

  - hard stop at -15% from the fill
  - at +35%: sell 75%
  - the remaining 25% trails 35% below its peak, never below entry
  - max hold 48h
  - rug guard: exit if pool liquidity halves (or the pool disappears)
  - per-book daily drawdown halt on new entries
"""
from __future__ import annotations

import logging
from collections import deque
import time
from datetime import datetime, timezone

from ..config import HARD_MAX_POSITION_PCT, Config
from ..http import DataError
from ..models import Kill, OrderTicket, Position, RiskApproval, require_stage

log = logging.getLogger("memedesk.risk")

EXIT_LABELS = {"stop_loss": "stop loss", "take_profit": "take profit 75%", "runner_trail": "trail exit",
               "runner_breakeven": "breakeven exit", "max_hold": "48h max hold", "liq_crash": "liquidity crash",
               "pool_vanished": "pool vanished", "dump_exit": "dump detected", "stale_rotate": "rotated out (stalled)",
               "chop_take_profit": "small profit, sideways",
               "spike_take_profit": "sold into spike",
               "ai_sell": "AI sold"}
PRICE_MISS_LIMIT = 20  # consecutive ticks with no price => pool treated as gone


def runner_stop(p: Position, cfg: Config) -> float:
    stop = p.peak_price * (1 - cfg.runner_trail_pct / 100)
    return max(stop, p.entry_price) if cfg.runner_floor_at_entry else stop


def step(p: Position, price: float, liq: float | None, now: float, cfg: Config
         ) -> tuple[tuple[str, float, str] | None, list[str]]:
    """Advance one position by one price tick.
    Returns ((action, fraction, reason) | None, events). Mutates peak/stop."""
    events: list[str] = []
    p.last_price = price
    p.peak_price = max(p.peak_price, price)
    gain = (price / p.entry_price - 1) * 100

    if p.tp_taken:
        new_stop = runner_stop(p, cfg)
        if new_stop > p.stop_price + 1e-15:
            p.stop_price = new_stop
            events.append(f"runner stop -> {new_stop:.8g} ({(new_stop / p.entry_price - 1) * 100:+.1f}%)")
    if price <= p.stop_price:
        if not p.tp_taken:
            return ("exit", 1.0, "stop_loss"), events
        floor = cfg.runner_floor_at_entry and p.stop_price <= p.entry_price * (1 + 1e-9)
        return ("exit", 1.0, "runner_breakeven" if floor else "runner_trail"), events
    if liq is not None and p.entry_liq_usd > 0 and liq < p.entry_liq_usd * (1 - cfg.liq_crash_exit_pct / 100):
        return ("exit", 1.0, "liq_crash"), events
    if cfg.max_hold_h > 0 and now - p.opened_ts > cfg.max_hold_h * 3600:
        return ("exit", 1.0, "max_hold"), events
    if not p.tp_taken and gain >= cfg.tp_trigger_pct:
        frac = cfg.tp_sell_pct / 100
        return ("exit" if frac >= 1 else "partial", frac, "take_profit"), events
    return None, events


def dump_check(p: Position, price: float, pair: dict, cfg: Config):
    """Early exit on a live dump: below entry, -10%+ over 5 min and sellers >= 2x buyers.
    Cuts the losers that would otherwise gap through the hard stop between price checks."""
    if not cfg.dump_exit or price >= p.entry_price:
        return None
    m5 = float((pair.get("priceChange") or {}).get("m5") or 0)
    tx = (pair.get("txns") or {}).get("m5") or {}
    b, s = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
    if m5 <= -cfg.dump_m5_pct and s >= max(5, cfg.dump_sell_ratio * b):
        return ("exit", 1.0, "dump_exit")
    return None


def spike_check(p: Position, price: float, hist, now: float, cfg: Config):
    """Momentum book: a vertical move (+15% inside 3 min) is where holders take profit. Sell most of it into
    the spike; if only the runner is left, pull its stop up tight under the price instead.
    Returns (action | None, event text | None)."""
    window = [px for ts, px in hist if now - ts <= cfg.mom_spike_window_s]
    if len(window) < 2 or min(window) <= 0:
        return None, None
    jump = (price / min(window) - 1) * 100
    if jump < cfg.mom_spike_pct:
        return None, None
    if not p.tp_taken:
        if (price / p.entry_price - 1) * 100 >= cfg.mom_spike_min_pnl_pct:
            return ("partial", cfg.mom_spike_sell_pct / 100, "spike_take_profit"), None
        return None, None
    tight = price * (1 - cfg.mom_spike_runner_stop_pct / 100)
    if tight > p.stop_price:
        p.stop_price = tight
        return None, f"spike +{jump:.0f}% in {cfg.mom_spike_window_s / 60:.0f}m: runner stop -> {tight:.8g}"
    return None, None


def chop_check(p: Position, price: float, now: float, cfg: Config):
    """Momentum book: a position that has only drifted a few % for 20+ min and is now slightly in profit
    (after exit slippage and fee) sells most of it and keeps a runner floored at entry."""
    if p.tp_taken or now - p.opened_ts < cfg.mom_chop_min * 60:
        return None
    if (p.peak_price / p.entry_price - 1) * 100 > cfg.mom_chop_max_peak_pct:
        return None                                  # it did run at some point: let the normal exits handle it
    slip = cfg.rules(p.book).slippage_bps / 1e4
    net = (price * (1 - slip) * (1 - cfg.paper_fee_bps / 1e4) / p.entry_price - 1) * 100
    if net >= cfg.mom_chop_min_net_pct:
        return ("partial", cfg.mom_chop_sell_pct / 100, "chop_take_profit")
    return None


def stale_check(p: Position, price: float, pair: dict, now: float, cfg: Config):
    """Momentum book: free the slot when a position has gone nowhere for an hour and its momentum is gone.
    Runners (after take-profit) are left alone."""
    if p.tp_taken or now - p.opened_ts < cfg.mom_stale_min * 60:
        return None
    pnl = (price / p.entry_price - 1) * 100
    if not cfg.mom_stale_low_pct <= pnl <= cfg.mom_stale_high_pct:
        return None
    m5 = float((pair.get("priceChange") or {}).get("m5") or 0)
    tx = (pair.get("txns") or {}).get("m5") or {}
    b, s = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
    if m5 < 0 or s >= b:
        return ("exit", 1.0, "stale_rotate")
    return None


class Risk:
    name = "risk"

    def __init__(self, cfg: Config, logger, dex):
        self.cfg, self.logger, self.dex = cfg, logger, dex
        self.quotes: dict[str, dict] = {}   # mint -> latest {price, mcap, h1, ts} for the dashboard
        self.stop_strikes: dict[str, int] = {}
        self.history: dict[str, deque] = {}  # position id -> recent (ts, price) samples (every monitor tick)

    # ---- pre-trade --------------------------------------------------------
    def halted(self, book: str, equity_usd: float) -> str | None:
        key = f"day_start:{book}:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = self.logger.get(key)
        if start is None:
            self.logger.put(key, equity_usd)
            return None
        dd = 100 * (1 - equity_usd / start) if start else 0.0
        if dd >= self.cfg.daily_loss_limit_pct:
            return f"{book} daily drawdown {dd:.1f}% >= {self.cfg.daily_loss_limit_pct:.0f}%"
        return None

    def pre_trade(self, t: OrderTicket, open_positions: list[Position]) -> RiskApproval | Kill:
        require_stage(t, "executor", self.name)

        def kill(reason, detail, cooldown=None):
            return Kill(self.name, t.mint, t.symbol, reason, detail, cooldown_min=cooldown, book=t.book)

        mine = [p for p in open_positions if p.book == t.book]
        if why := self.halted(t.book, t.equity_usd):
            return kill("halted", why, cooldown=30)
        rules = self.cfg.rules(t.book)
        if len(mine) >= rules.max_open_positions:
            return kill("max_positions", f"{len(mine)} open in {t.book}", cooldown=5)
        addon = bool(getattr(t.pick, "addon_of", ""))
        if any(p.mint == t.mint for p in mine) and not addon:
            return kill("already_holding", f"already open in {t.book}", cooldown=60)
        cap = t.equity_usd * HARD_MAX_POSITION_PCT / 100
        if t.size_usd > cap + 1e-6:
            return kill("oversize", f"${t.size_usd:.2f} > 8% cap ${cap:.2f}")
        stop = self.cfg.mom_readd_stop_pct if addon else rules.stop_pct
        return RiskApproval(ticket_id=t.id, stop_pct=stop, max_size_usd=min(cap, t.size_usd))

    # ---- live management --------------------------------------------------
    def _confirm_stop(self, p: Position, price: float, action):
        """Momentum: a one-tick wick through the hard stop doesn't sell; two checks in a row below it do,
        and a deep gap (>= 8% under the stop) sells at once."""
        if not action or action[2] != "stop_loss":
            self.stop_strikes.pop(p.id, None)
            return action
        if price <= p.stop_price * (1 - self.cfg.mom_stop_gap_pct / 100):
            self.stop_strikes.pop(p.id, None)
            return action
        n = self.stop_strikes.get(p.id, 0) + 1
        if n >= self.cfg.mom_stop_confirm_ticks:
            self.stop_strikes.pop(p.id, None)
            return action
        self.stop_strikes[p.id] = n
        self.logger.event(self.name, "stop_move", p.mint, p.symbol, p.book,
                          f"price under stop ({price:.8g} < {p.stop_price:.8g}): confirming on next check")
        return None

    def monitor(self, executor, lock) -> None:
        open_pos = self.logger.positions("open")
        if not open_pos:
            return
        try:
            quotes = self.dex.prices({p.mint for p in open_pos})
        except DataError as e:
            log.warning("monitor price fetch failed: %s", e)
            return
        now = time.time()
        for m, q in quotes.items():
            self.quotes[m] = {"price": q["price"], "mcap": q.get("mcap"), "h1": q.get("h1"),
                              "symbol": q.get("symbol", ""), "image": q.get("image", ""), "ts": now}
        for p in open_pos:
            q = quotes.get(p.mint)
            if not q or q["price"] <= 0:
                p.price_misses += 1
                self.logger.save_position(p)
                if p.price_misses < PRICE_MISS_LIMIT:
                    continue
                action, price = ("exit", 1.0, "pool_vanished"), 0.0
            else:
                p.price_misses = 0
                price = q["price"]
                action, events = step(p, price, q["liq"], now, self.cfg.rules(p.book))
                if p.book == "momentum":
                    action = self._confirm_stop(p, price, action)
                if action is None and p.book != "ai":
                    action = dump_check(p, price, q.get("pair") or {}, self.cfg)
                hist = self.history.setdefault(p.id, deque(maxlen=200))
                hist.append((now, price))
                if action is None and p.book == "momentum":
                    action, ev = spike_check(p, price, hist, now, self.cfg)
                    if ev:
                        self.logger.event(self.name, "stop_move", p.mint, p.symbol, p.book, ev)
                if action is None and p.book == "momentum":
                    action = chop_check(p, price, now, self.cfg)
                if action is None and p.book == "momentum":
                    action = stale_check(p, price, q.get("pair") or {}, now, self.cfg)
                for e in events:
                    self.logger.event(self.name, "stop_move", p.mint, p.symbol, p.book, e)
                self.logger.save_position(p)
            if action:
                self._act(executor, lock, p, action, price, now)

    def _act(self, executor, lock, p: Position, action, price: float, now: float) -> None:
        kind, frac, reason = action
        cost_part = p.cost_usd * (p.qty / p.initial_qty if p.initial_qty else 1) * (frac if frac < 1 else 1)
        before = p.realized_usd
        with lock:
            try:
                q = self.quotes.get(p.mint) or {}
                p = executor.sell(p, frac, price, reason, liq_usd=q.get("liq"), mcap=q.get("mcap"))
            except Exception as e:  # keep the desk alive; retry next tick
                self.logger.event(self.name, "error", p.mint, p.symbol, "sell_failed", str(e),
                                  level=logging.ERROR)
                return
            if kind == "partial":
                p.tp_taken = True
                p.stop_price = runner_stop(p, self.cfg.rules(p.book))
                self.logger.save_position(p)
        leg_pnl = (p.realized_usd - before) - cost_part
        label = (f"take profit {self.cfg.rules(p.book).tp_sell_pct:g}%" if reason == "take_profit"
                 else f"small profit {frac * 100:.0f}%, sideways" if reason == "chop_take_profit"
                 else f"sold {frac * 100:.0f}% into spike" if reason == "spike_take_profit"
                 else EXIT_LABELS.get(reason, reason))
        self.logger.event(self.name, "decision", p.mint, p.symbol, "SELL", f"{label} {leg_pnl:+.2f}$",
                          data={"side": "SELL", "book": p.book, "reason": f"{label} {'+' if leg_pnl >= 0 else '−'}${abs(leg_pnl):.2f}",
                                "usd": round(p.realized_usd - before, 2), "pnl_usd": round(leg_pnl, 2)})
        if kind == "partial":
            msg = (f"TP {p.symbol} [{p.book}] sold {frac * 100:.0f}% at "
                   f"+{(price / p.entry_price - 1) * 100:.0f}%, runner stop {p.stop_price:.8g}")
            self.logger.event(self.name, "partial", p.mint, p.symbol, reason, msg,
                              data={"position": p.to_dict()}, level=logging.WARNING)
            self.logger.notify(msg)
            return
        msg = f"EXIT {p.symbol} [{p.book}] {reason} {p.pnl_pct:+.1f}% (${p.pnl_usd:+.2f})"
        self.logger.event(self.name, "exit", p.mint, p.symbol, reason, msg,
                          data={"position": p.to_dict()}, level=logging.WARNING)
        self.logger.notify(msg)
        if p.book == "momentum":
            cool_min = (self.cfg.mom_reentry_cooldown_min if (p.pnl_usd or 0) >= 0
                        else self.cfg.mom_loss_cooldown_min)
        else:
            cool_min = self.cfg.reentry_cooldown_h * 60
        self.logger.set_cooldown(p.mint, cool_min, self.name, "reentry", book=p.book)
        self.history.pop(p.id, None)
        if reason in ("liq_crash", "pool_vanished") and p.creator:
            self.logger.put(f"rugger:{p.creator}", {"mint": p.mint, "ts": now})
