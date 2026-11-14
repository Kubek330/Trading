"""Typed handoff objects. Each agent accepts exactly one type and returns the
next type or a Kill. The `trail` on every object records which agents touched
it, and each agent refuses input that didn't come from the agent before it."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any


class HandoffError(RuntimeError):
    """An agent got an object that skipped a stage."""


def require_stage(obj: Any, expected_last: str | tuple[str, ...], agent: str) -> None:
    allowed = (expected_last,) if isinstance(expected_last, str) else expected_last
    trail = getattr(obj, "trail", None)
    if not trail or trail[-1] not in allowed:
        raise HandoffError(f"{agent} expected input from {'/'.join(allowed)}, got trail={trail!r}")


@dataclass
class Kill:
    agent: str
    mint: str
    symbol: str
    reason: str            # short machine code, e.g. "mint_authority_live"
    detail: str            # human-readable
    data: dict = field(default_factory=dict)
    cooldown_min: float | None = None
    book: str | None = None  # None = desk-wide (scout/filter); else scoped to that book
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
    reason: str = ""                 # why it was picked up (whale signal text)
    image: str = ""                  # token logo URL from DexScreener (display only)
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
    top10_pct: float | None = None
    top_holder_owners: list[str] = field(default_factory=list)
    rugcheck_score: float | None = None
    organic_score: float | None = None
    organic_label: str | None = None


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
    book: str = "scored"


@dataclass
class RandomPick:
    """Random-control output: a vetted candidate picked without scoring."""
    vetted: Vetted
    index: int                       # position in the cycle's vetted pool, for audit
    pool_size: int
    trail: list[str] = field(default_factory=list)
    book: str = "random"
    score: float | None = None


@dataclass
class OrderTicket:
    """Executor proposal. Not fillable until Risk attaches an approval."""
    pick: Scorecard | RandomPick
    book: str
    size_usd: float
    ref_price: float
    equity_usd: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trail: list[str] = field(default_factory=list)
    created_ts: float = field(default_factory=time.time)

    @property
    def mint(self) -> str:
        return self.pick.vetted.candidate.mint

    @property
    def symbol(self) -> str:
        return self.pick.vetted.candidate.symbol


@dataclass
class RiskApproval:
    ticket_id: str
    stop_pct: float
    max_size_usd: float
    trail: list[str] = field(default_factory=lambda: ["risk"])
    ts: float = field(default_factory=time.time)


@dataclass
class Fill:
    price: float           # effective USD price per token, fees and slippage included
    qty: float             # tokens
    usd: float             # USD spent (buy) or received (sell)
    tx: str = "paper"


@dataclass
class Position:
    id: str
    book: str
    mint: str
    symbol: str
    pair_address: str
    entry_price: float
    initial_qty: float
    qty: float             # still held
    cost_usd: float
    stop_price: float
    peak_price: float
    entry_liq_usd: float
    score: float | None
    opened_ts: float
    tp_taken: bool = False
    realized_usd: float = 0.0          # proceeds from all sells so far
    status: str = "open"
    exit_price: float | None = None    # price of the final sell
    exit_ts: float | None = None
    exit_reason: str | None = None
    last_price: float | None = None
    creator: str = ""
    price_misses: int = 0
    sells: list = field(default_factory=list)   # [{ts, qty, price, usd, reason}]

    @property
    def pnl_usd(self) -> float | None:
        return None if self.status != "closed" else self.realized_usd - self.cost_usd

    @property
    def pnl_pct(self) -> float | None:
        p = self.pnl_usd
        return None if p is None or not self.cost_usd else 100 * p / self.cost_usd

    @property
    def value_usd(self) -> float:
        return self.qty * (self.last_price or self.entry_price)

    @property
    def unrealized_pct(self) -> float:
        return 100 * ((self.realized_usd + self.value_usd) / self.cost_usd - 1) if self.cost_usd else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
