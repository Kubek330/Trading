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
import time
from datetime import datetime, timezone

from ..config import HARD_MAX_POSITION_PCT, Config
from ..http import DataError
from ..models import Kill, OrderTicket, Position, RiskApproval, require_stage

log = logging.getLogger("memedesk.risk")

EXIT_LABELS = {"stop_loss": "stop loss", "take_profit": "take profit 75%", "runner_trail": "trail exit",
               "runner_breakeven": "breakeven exit", "max_hold": "48h max hold", "liq_crash": "liquidity crash",
               "pool_vanished": "pool vanished"}
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


class Risk:
    name = "risk"

    def __init__(self, cfg: Config, logger, dex):
        self.cfg, self.logger, self.dex = cfg, logger, dex
        self.quotes: dict[str, dict] = {}   # mint -> latest {price, mcap, h1, ts} for the dashboard

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
        if len(mine) >= self.cfg.max_open_positions:
            return kill("max_positions", f"{len(mine)} open in {t.book}", cooldown=5)
        if any(p.mint == t.mint for p in mine):
            return kill("already_holding", f"already open in {t.book}", cooldown=60)
        cap = t.equity_usd * HARD_MAX_POSITION_PCT / 100
        if t.size_usd > cap + 1e-6:
            return kill("oversize", f"${t.size_usd:.2f} > 8% cap ${cap:.2f}")
        return RiskApproval(ticket_id=t.id, stop_pct=self.cfg.stop_pct, max_size_usd=min(cap, t.size_usd))

    # ---- live management --------------------------------------------------
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
                action, events = step(p, price, q["liq"], now, self.cfg)
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
                p = executor.sell(p, frac, price, reason)
            except Exception as e:  # keep the desk alive; retry next tick
                self.logger.event(self.name, "error", p.mint, p.symbol, "sell_failed", str(e),
                                  level=logging.ERROR)
                return
            if kind == "partial":
                p.tp_taken = True
                p.stop_price = runner_stop(p, self.cfg)
                self.logger.save_position(p)
        leg_pnl = (p.realized_usd - before) - cost_part
        label = (f"take profit {self.cfg.tp_sell_pct:g}%" if reason == "take_profit"
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
        self.logger.set_cooldown(p.mint, self.cfg.reentry_cooldown_h * 60, self.name, "reentry", book=p.book)
        if reason in ("liq_crash", "pool_vanished") and p.creator:
            self.logger.put(f"rugger:{p.creator}", {"mint": p.mint, "ts": now})
