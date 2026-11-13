"""Executor: sizes a ticket (max 8% of equity), and fills it only after Risk
has attached a stop. It also closes positions when Risk says so."""
from __future__ import annotations

import logging
import time
import uuid

from ..config import HARD_MAX_POSITION_PCT, Config
from ..http import DataError
from ..models import (Fill, HandoffError, Kill, OrderTicket, Position, RiskApproval,
                      Scorecard, require_stage)

log = logging.getLogger("memedesk.executor")

TICKET_TTL_S = 60.0


class Executor:
    name = "executor"

    def __init__(self, cfg: Config, broker, dex, logger):
        self.cfg, self.broker, self.dex, self.logger = cfg, broker, dex, logger

    def propose(self, sc: Scorecard, equity_usd: float, cash_usd: float) -> OrderTicket | Kill:
        require_stage(sc, "analyst", self.name)
        c = sc.vetted.candidate

        def kill(reason, detail, cooldown=None):
            return Kill(self.name, c.mint, c.symbol, reason, detail, cooldown_min=cooldown)

        try:
            q = self.dex.prices([c.mint]).get(c.mint)
        except DataError as e:
            return kill("no_requote", str(e), cooldown=2)
        if not q or q["price"] <= 0:
            return kill("no_requote", "no live price", cooldown=2)
        px, liq = q["price"], q["liq"]
        drift = 100 * (px / c.price_usd - 1) if c.price_usd else 0.0
        if abs(drift) > self.cfg.max_price_drift_pct:
            return kill("price_moved", f"moved {drift:+.1f}% since scoring", cooldown=5)
        h1 = float((q["pair"].get("priceChange") or {}).get("h1") or 0)
        if h1 > self.cfg.max_h1_change_pct:
            return kill("pumped_1h", f"+{h1:.0f}% in 1h at requote", cooldown=60)

        pct = min(self.cfg.position_pct, HARD_MAX_POSITION_PCT)
        size = equity_usd * pct / 100
        size = min(size, liq * self.cfg.max_pool_impact_pct / 100, cash_usd)
        if size < self.cfg.min_trade_usd:
            return kill("size_too_small", f"size ${size:.2f} (cash ${cash_usd:.2f}, liq ${liq:,.0f})", cooldown=5)
        return OrderTicket(scorecard=sc, size_usd=round(size, 2), ref_price=px,
                           equity_usd=equity_usd, trail=sc.trail + [self.name])

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

        c = t.scorecard.vetted.candidate
        fill: Fill = self.broker.buy(c.mint, t.size_usd, t.ref_price)
        stop = fill.price * (1 - a.stop_pct / 100)
        pos = Position(
            id=uuid.uuid4().hex[:12], mint=c.mint, symbol=c.symbol, pair_address=c.pair_address,
            mode=self.broker.mode, entry_price=fill.price, qty=fill.qty, cost_usd=fill.usd,
            stop_price=stop, stop_pct=a.stop_pct, trail_activate_pct=a.trail_activate_pct,
            trail_pct=a.trail_pct, peak_price=fill.price, entry_liq_usd=c.liquidity_usd,
            score=t.scorecard.score, opened_ts=time.time(), last_price=fill.price,
            entry_tx=fill.tx, creator=t.scorecard.vetted.facts.creator or "",
        )
        self.logger.save_position(pos)
        return pos

    def close(self, pos: Position, ref_price: float, reason: str) -> Position:
        fill = self.broker.sell(pos, ref_price)
        pos.status, pos.exit_price, pos.exit_ts = "closed", fill.price, time.time()
        pos.exit_reason, pos.proceeds_usd, pos.exit_tx = reason, fill.usd, fill.tx
        self.logger.save_position(pos)
        return pos
