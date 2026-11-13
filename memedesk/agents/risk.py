"""Risk: approves every ticket with a stop attached before entry (-6%), then
manages open positions: hard stop, trailing stop once +12%, liquidity-crash
exit, max hold time, and a daily drawdown halt on new entries."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ..config import HARD_MAX_POSITION_PCT, Config
from ..http import DataError
from ..models import Kill, OrderTicket, Position, RiskApproval, require_stage

log = logging.getLogger("memedesk.risk")

PRICE_MISS_LIMIT = 30  # consecutive monitor ticks with no price => treat pool as gone


def step(p: Position, price: float, liq: float | None, now: float, cfg: Config) -> tuple[str | None, list[str]]:
    """Advance one position by one price tick. Returns (exit_reason|None, events)."""
    events: list[str] = []
    p.last_price = price
    if price > p.peak_price:
        p.peak_price = price
    if not p.trailing and (p.peak_price / p.entry_price - 1) * 100 >= p.trail_activate_pct:
        p.trailing = True
        events.append(f"trail armed at +{(p.peak_price / p.entry_price - 1) * 100:.1f}%")
    if p.trailing:
        new_stop = p.peak_price * (1 - p.trail_pct / 100)
        if new_stop > p.stop_price:
            p.stop_price = new_stop
            events.append(f"stop -> {new_stop:.8g} ({(new_stop / p.entry_price - 1) * 100:+.1f}% vs entry)")
    if price <= p.stop_price:
        return ("trail_stop" if p.trailing else "stop_loss"), events
    if liq is not None and p.entry_liq_usd > 0 and liq < p.entry_liq_usd * (1 - cfg.liq_crash_exit_pct / 100):
        return "liq_crash", events
    if cfg.max_hold_h > 0 and now - p.opened_ts > cfg.max_hold_h * 3600:
        return "max_hold", events
    return None, events


class Risk:
    name = "risk"

    def __init__(self, cfg: Config, logger, dex):
        self.cfg, self.logger, self.dex = cfg, logger, dex

    # ---- pre-trade --------------------------------------------------------
    def _day_key(self) -> str:
        return "day_start_equity:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def halted(self, equity_usd: float) -> str | None:
        key = self._day_key()
        start = self.logger.get(key)
        if start is None:
            self.logger.put(key, equity_usd)
            return None
        dd = 100 * (1 - equity_usd / start) if start else 0.0
        if dd >= self.cfg.daily_loss_limit_pct:
            return f"daily drawdown {dd:.1f}% >= {self.cfg.daily_loss_limit_pct:.0f}%"
        return None

    def pre_trade(self, t: OrderTicket, open_positions: list[Position]) -> RiskApproval | Kill:
        require_stage(t, "executor", self.name)

        def kill(reason, detail, cooldown=None):
            return Kill(self.name, t.mint, t.symbol, reason, detail, cooldown_min=cooldown)

        if why := self.halted(t.equity_usd):
            return kill("halted", why, cooldown=30)
        if len(open_positions) >= self.cfg.max_open_positions:
            return kill("max_positions", f"{len(open_positions)} open", cooldown=5)
        if any(p.mint == t.mint for p in open_positions):
            return kill("already_holding", "position already open", cooldown=60)
        cap = t.equity_usd * HARD_MAX_POSITION_PCT / 100
        if t.size_usd > cap + 1e-6:
            return kill("oversize", f"${t.size_usd:.2f} > 8% cap ${cap:.2f}")
        stop_px = t.ref_price * (1 - self.cfg.stop_pct / 100)
        if stop_px <= 0:
            return kill("no_stop", "could not place stop")
        return RiskApproval(ticket_id=t.id, stop_pct=self.cfg.stop_pct,
                            trail_activate_pct=self.cfg.trail_activate_pct,
                            trail_pct=self.cfg.trail_pct, max_size_usd=min(cap, t.size_usd))

    # ---- live management --------------------------------------------------
    def monitor(self, executor, lock) -> None:
        open_pos = self.logger.positions("open")
        if not open_pos:
            return
        try:
            quotes = self.dex.prices([p.mint for p in open_pos])
        except DataError as e:
            log.warning("monitor price fetch failed: %s", e)
            return
        now = time.time()
        for p in open_pos:
            q = quotes.get(p.mint)
            if not q or q["price"] <= 0:
                p.price_misses += 1
                self.logger.save_position(p)
                if p.price_misses < PRICE_MISS_LIMIT:
                    continue
                reason, price = "pool_vanished", 0.0
            else:
                p.price_misses = 0
                price = q["price"]
                reason, events = step(p, price, q["liq"], now, self.cfg)
                for e in events:
                    self.logger.event(self.name, "stop_move", p.mint, p.symbol, "trail", e)
                self.logger.save_position(p)
            if reason:
                with lock:
                    try:
                        closed = executor.close(p, price, reason)
                    except Exception as e:  # keep the desk alive; retry next tick
                        self.logger.event(self.name, "error", p.mint, p.symbol, "close_failed", str(e),
                                          level=logging.ERROR)
                        continue
                pnl = closed.pnl_usd or 0.0
                pct = 100 * pnl / closed.cost_usd if closed.cost_usd else 0.0
                msg = f"EXIT {closed.symbol} {reason} {pct:+.1f}% (${pnl:+.2f})"
                self.logger.event(self.name, "exit", p.mint, p.symbol, reason, msg,
                                  data={"position": closed.to_dict()}, level=logging.WARNING)
                self.logger.notify(f"[{closed.mode}] {msg}")
                self.logger.set_cooldown(p.mint, self.cfg.reentry_cooldown_h, self.name, "reentry")
                if reason in ("liq_crash", "pool_vanished") and closed.creator:
                    self.logger.put(f"rugger:{closed.creator}", {"mint": p.mint, "ts": now})
