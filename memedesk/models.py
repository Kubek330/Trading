"""Typed handoff objects. Each agent accepts exactly one type and returns the
next type or a Kill. The `trail` on every object records which agents touched
it, and each agent refuses input that didn't come from the agent before it."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


class HandoffError(RuntimeError):
    """An agent got an object that skipped a stage."""


def require_stage(obj: Any, expected_last: str, agent: str) -> None:
    trail = getattr(obj, "trail", None)
    if not trail or trail[-1] != expected_last:
        raise HandoffError(
            f"{agent} expected input from {expected_last}, got trail={trail!r}"
        )


@dataclass
class Kill:
    agent: str
    mint: str
    symbol: str
    reason: str            # short machine code, e.g. "mint_authority_live"
    detail: str            # human-readable
    data: dict = field(default_factory=dict)
    cooldown_min: float | None = None
    ts: float = field(default_factory=time.time)


@dataclass
class Candidate:
    """Scout output: one DexScreener pair inside the age/mcap window."""
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    quote_symbol: str
    price_usd: float
    mcap: float
    liquidity_usd: float
    pair_created_ms: int
    price_change: dict[str, float]
    txns: dict[str, dict[str, int]]
    volume: dict[str, float]
    sources: list[str] = field(default_factory=list)
    trail: list[str] = field(default_factory=lambda: ["scout"])
    seen_ts: float = field(default_factory=time.time)

    @property
    def age_h(self) -> float:
        return max(0.0, (self.seen_ts * 1000 - self.pair_created_ms) / 3_600_000)

    @property
    def liq_mcap_pct(self) -> float:
        return 100.0 * self.liquidity_usd / self.mcap if self.mcap > 0 else 0.0


@dataclass
class SafetyFacts:
    mint_authority: str | None
    freeze_authority: str | None
    lp_locked_pct: float | None
    dev_pct: float | None
    creator: str | None
    creator_rugs: int | None
    bundle_pct: float | None
    bundle_source: str | None
    holders: int | None
    top_holder_owners: list[str] = field(default_factory=list)
    rugcheck_score: float | None = None


@dataclass
class Vetted:
    """Filter output: candidate that survived every kill rule."""
    candidate: Candidate
    facts: SafetyFacts
    trail: list[str] = field(default_factory=list)


@dataclass
class Scorecard:
    """Analyst output: a vetted candidate that scored >= min_score."""
    vetted: Vetted
    score: float
    components: dict[str, float]
    notes: list[str] = field(default_factory=list)
    grok_note: str = ""
    trail: list[str] = field(default_factory=list)


@dataclass
class OrderTicket:
    """Executor proposal. Not fillable until Risk attaches an approval."""
    scorecard: Scorecard
    size_usd: float
    ref_price: float
    equity_usd: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trail: list[str] = field(default_factory=list)
    created_ts: float = field(default_factory=time.time)

    @property
    def mint(self) -> str:
        return self.scorecard.vetted.candidate.mint

    @property
    def symbol(self) -> str:
        return self.scorecard.vetted.candidate.symbol


@dataclass
class RiskApproval:
    ticket_id: str
    stop_pct: float
    trail_activate_pct: float
    trail_pct: float
    max_size_usd: float
    trail: list[str] = field(default_factory=lambda: ["risk"])
    ts: float = field(default_factory=time.time)


@dataclass
class Fill:
    price: float           # effective USD price per token, fees and slippage included
    qty: float             # tokens
    usd: float             # USD spent (buy) or received (sell)
    tx: str = ""


@dataclass
class Position:
    id: str
    mint: str
    symbol: str
    pair_address: str
    mode: str
    entry_price: float
    qty: float
    cost_usd: float
    stop_price: float
    stop_pct: float
    trail_activate_pct: float
    trail_pct: float
    peak_price: float
    entry_liq_usd: float
    score: float
    opened_ts: float
    trailing: bool = False
    status: str = "open"
    exit_price: float | None = None
    exit_ts: float | None = None
    exit_reason: str | None = None
    proceeds_usd: float | None = None
    last_price: float | None = None
    entry_tx: str = ""
    exit_tx: str = ""
    creator: str = ""
    price_misses: int = 0

    @property
    def pnl_usd(self) -> float | None:
        if self.proceeds_usd is None:
            return None
        return self.proceeds_usd - self.cost_usd

    def to_dict(self) -> dict:
        return asdict(self)
