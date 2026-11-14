"""Sniper: a separate experiment that hunts brand-new coins (3-30 min old).

Flow (hard handoffs, own paper book "sniper"):
  scout (young coins it saw) -> sniper.prefilter (cheap momentum gate on DexScreener data)
  -> filter (same on-chain safety checks, sniper thresholds) -> sniper.pick -> executor -> risk

Young coins are tracked and re-checked every scan until they turn 30 min, so a coin
that starts moving at minute 8 is still caught. Its kills are scoped to the sniper
book, so they never block the main desk from looking at the same coin later.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Config
from ..models import Candidate, Kill, Vetted, require_stage


@dataclass
class SniperPick:
    vetted: Vetted
    momentum: float                   # 0-100, for ordering and the decision text
    trail: list[str] = field(default_factory=list)
    book: str = "sniper"
    score: float | None = None


def momentum_score(c: Candidate) -> float:
    tx = c.txns.get("m5", {})
    b, s = tx.get("buys", 0), tx.get("sells", 0)
    ratio = b / s if s else (3.0 if b else 0.0)
    m5 = c.price_change.get("m5", 0.0)
    act = min(1.0, (b + s) / 120)
    return round(100 * (0.4 * min(1.0, (ratio - 1) / 1.5) + 0.3 * act + 0.3 * min(1.0, max(0.0, m5) / 25)), 1)


class Sniper:
    name = "sniper"
    book = "sniper"

    def __init__(self, cfg: Config, dex, logger):
        self.cfg, self.dex, self.logger = cfg, dex, logger
        self.track: dict[str, float] = {}   # mint -> first seen

    def candidates(self, young: list[Candidate]) -> list[Candidate]:
        """This scan's young coins plus tracked ones refreshed from DexScreener."""
        now, cfg = time.time(), self.cfg
        for c in young:
            self.track.setdefault(c.mint, now)
        fresh = {c.mint: c for c in young}
        stale = [m for m in self.track if m not in fresh]
        if stale:
            from .scout import candidate_from_pair
            try:
                best = self.dex.best_pair_per_mint(self.dex.pairs_for_mints(stale[: cfg.sniper_track_max]), stale)
            except Exception:
                best = {}
            for m, p in best.items():
                fresh[m] = candidate_from_pair(p, ["sniper-track"], now)
        out = []
        for m, c in fresh.items():
            if c.age_h * 60 > cfg.sniper_max_age_min:
                self.track.pop(m, None)          # grown up: the main desk's watchlist takes over
                continue
            out.append(c)
        # oldest-first eviction keeps the tracker bounded
        if len(self.track) > cfg.sniper_track_max * 2:
            for m in sorted(self.track, key=self.track.get)[: len(self.track) - cfg.sniper_track_max * 2]:
                del self.track[m]
        return out

    def prefilter(self, c: Candidate) -> Kill | None:
        cfg = self.cfg
        require_stage(c, "scout", self.name)

        def kill(reason, detail, cooldown=2):
            return Kill(self.name, c.mint, c.symbol, reason, detail, cooldown_min=cooldown, book=self.book)

        age_min = c.age_h * 60
        if age_min < cfg.sniper_min_age_min:
            return kill("too_fresh", f"age {age_min:.1f}m < {cfg.sniper_min_age_min:g}m", 1)
        if not cfg.sniper_min_mcap <= c.mcap <= cfg.sniper_max_mcap:
            return kill("mcap_out", f"mcap ${c.mcap:,.0f}", 3)
        if c.liquidity_usd < cfg.sniper_min_liq:
            return kill("liq_low", f"liq ${c.liquidity_usd:,.0f} < ${cfg.sniper_min_liq:,.0f}", 3)
        tx = c.txns.get("m5", {})
        b, s = tx.get("buys", 0), tx.get("sells", 0)
        if b + s < cfg.sniper_min_txns_m5:
            return kill("quiet", f"{b + s} txns in 5m < {cfg.sniper_min_txns_m5}")
        ratio = b / s if s else float("inf")
        if ratio < cfg.sniper_min_buy_sell_m5:
            return kill("sellers", f"buy/sell 5m {ratio:.2f} < {cfg.sniper_min_buy_sell_m5:g}")
        m5 = c.price_change.get("m5", 0.0)
        if m5 > cfg.sniper_max_m5_pct:
            return kill("vertical", f"+{m5:.0f}% in 5m > {cfg.sniper_max_m5_pct:g}%")
        if m5 < 0:
            return kill("falling", f"{m5:.0f}% in 5m")
        return None

    def pick(self, v: Vetted) -> SniperPick:
        require_stage(v, "filter", self.name)
        return SniperPick(vetted=v, momentum=momentum_score(v.candidate), trail=v.trail + [self.name])
