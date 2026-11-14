"""Executor: sizes a ticket (max 8% of the book's equity) and fills it only
after Risk has attached a stop. It also sells (in part or in full) when Risk says so.
Paper only."""
from __future__ import annotations

import logging
import time
import uuid

from ..config import HARD_MAX_POSITION_PCT, Config
from ..http import DataError
from ..models import (HandoffError, Kill, OrderTicket, Position, RandomPick, RiskApproval,
                      Scorecard, require_stage)

log = logging.getLogger("memedesk.executor")

TICKET_TTL_S = 60.0
UPSTREAM = {"scored": "analyst", "random": "random", "sniper": "sniper"}


class Executor:
    name = "executor"

    def __init__(self, cfg: Config, broker, dex, logger):
        self.cfg, self.broker, self.dex, self.logger = cfg, broker, dex, logger

    def propose(self, pick: Scorecard | RandomPick, equity_usd: float, cash_usd: float) -> OrderTicket | Kill:
        book = pick.book
        if book not in UPSTREAM:
            raise HandoffError(f"executor: unknown book {book!r}")
        require_stage(pick, UPSTREAM[book], self.name)
        c = pick.vetted.candidate

        def kill(reason, detail, cooldown=None):
            return Kill(self.name, c.mint, c.symbol, reason, detail, cooldown_min=cooldown, book=book)

        try:
            q = self.dex.prices([c.mint]).get(c.mint)
        except DataError as e:
            return kill("no_requote", str(e), cooldown=2)
        if not q or q["price"] <= 0:
            return kill("no_requote", "no live price", cooldown=2)
        px, liq = q["price"], q["liq"]
        drift = 100 * (px / c.price_usd - 1) if c.price_usd else 0.0
        if abs(drift) > self.cfg.max_price_drift_pct:
            return kill("price_moved", f"moved {drift:+.1f}% since vetting", cooldown=5)
        h1 = float((q["pair"].get("priceChange") or {}).get("h1") or 0)
        if book != "sniper" and h1 > self.cfg.max_h1_change_pct:  # sniper judges brand-new coins by its own m5 rules
            return kill("pumped_1h", f"+{h1:.0f}% in 1h at requote", cooldown=60)

        pct = min(self.cfg.rules(book).position_pct, HARD_MAX_POSITION_PCT)
        size = min(equity_usd * pct / 100, liq * self.cfg.max_pool_impact_pct / 100, cash_usd)
        if size < self.cfg.min_trade_usd:
            return kill("size_too_small", f"size ${size:.2f} (cash ${cash_usd:.2f}, liq ${liq:,.0f})", cooldown=5)
        return OrderTicket(pick=pick, book=book, size_usd=round(size, 2), ref_price=px,
                           equity_usd=equity_usd, trail=pick.trail + [self.name])

    def execute(self, t: OrderTicket, a: RiskApproval) -> Position:
        require_stage(t, "executor", self.name)
        if not isinstance(a, RiskApproval) or a.trail[-1:] != ["risk"] or a.ticket_id != t.id:
            raise HandoffError("executor: ticket has no matching risk approval")
        if a.stop_pct <= 0:
            raise HandoffError("executor: approval carries no stop")
        if t.size_usd > a.max_size_usd + 1e-6:
            raise HandoffError(f"executor: size {t.size_usd} over risk cap {a.max_size_usd}")
        if time.time() - t.created_ts > TICKET_TTL_S:
            raise HandoffError("executor: ticket expired")

        c = t.pick.vetted.candidate
        fill = self.broker.buy(t.book, t.size_usd, t.ref_price)
        pos = Position(
            id=uuid.uuid4().hex[:12], book=t.book, mint=c.mint, symbol=c.symbol,
            pair_address=c.pair_address, entry_price=fill.price, initial_qty=fill.qty, qty=fill.qty,
            cost_usd=fill.usd, stop_price=fill.price * (1 - a.stop_pct / 100),
            peak_price=fill.price, entry_liq_usd=c.liquidity_usd, score=t.pick.score,
            opened_ts=time.time(), last_price=fill.price,
            creator=t.pick.vetted.facts.creator or "",
        )
        self.logger.save_position(pos)
        return pos

    def sell(self, pos: Position, fraction: float, ref_price: float, reason: str) -> Position:
        """Sell `fraction` of what is still held. fraction=1 closes the position."""
        qty = pos.qty if fraction >= 1 else pos.qty * fraction
        fill = self.broker.sell(pos.book, qty, ref_price)
        pos.qty -= qty
        pos.realized_usd += fill.usd
        pos.sells.append({"ts": time.time(), "qty": qty, "price": fill.price,
                          "usd": round(fill.usd, 4), "reason": reason})
        if fraction >= 1 or pos.qty <= pos.initial_qty * 1e-9:
            pos.qty = 0.0
            pos.status, pos.exit_price, pos.exit_ts, pos.exit_reason = "closed", fill.price, time.time(), reason
        self.logger.save_position(pos)
        return pos
