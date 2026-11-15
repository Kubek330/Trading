"""Momentum: a separate experiment that buys often, like a simple "buy what's moving" bot.

No Analyst score, no holder-growth wait, no fake-volume check: only hard safety
(authorities, LP or bonding curve, not rugged, sane holders/dev) and live momentum
on DexScreener data. It answers one question on the dashboard: does buying often
make money after slippage and fees, or do the stricter books do better?

Flow: scout (every coin it saw <= 72h) -> momentum.prefilter -> filter (hard safety,
momentum thresholds) -> momentum.pick -> executor -> risk. Own book "momentum".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..models import Candidate, Kill, Vetted, require_stage
from .sniper import SKIP, momentum_score


@dataclass
class MomentumPick:
    vetted: Vetted
    momentum: float
    trail: list[str] = field(default_factory=list)
    book: str = "momentum"
    score: float | None = None
    addon_of: str = ""                   # re-add into a runner: parent position id
    size_cap_usd: float | None = None


def mom_quality(c: Candidate, cfg: Config) -> float:
    """Momentum score plus a small bonus for being on a trending list (attention), never a reason on its own."""
    return momentum_score(c) + (cfg.mom_trending_bonus if "trending" in (c.sources or []) else 0.0)


class Momentum:
    name = "momentum"
    book = "momentum"

    def __init__(self, cfg: Config, dex=None):
        self.cfg, self.dex = cfg, dex
        self.track: dict[str, float] = {}       # mint -> first seen
        self.tags: dict[str, set[str]] = {}     # mint -> discovery tags (e.g. "trending")

    def candidates(self, seeds: list[Candidate]) -> list[Candidate]:
        """Every coin seen recently, re-checked each scan: this scan's coins plus tracked ones refreshed from
        DexScreener. A coin that starts moving between scans is caught, not missed for 30 min."""
        import time as _t
        cfg, now = self.cfg, _t.time()
        fresh: dict[str, Candidate] = {}
        for c in seeds:
            if c.mint and c.age_h <= cfg.mom_max_age_h:
                self.track.setdefault(c.mint, now)
                self.tags.setdefault(c.mint, set()).update(c.sources or [])
                fresh.setdefault(c.mint, c)
        for m in [m for m, t0 in self.track.items() if now - t0 > cfg.mom_track_hours * 3600]:
            self.track.pop(m, None)
            self.tags.pop(m, None)
        if len(self.track) > cfg.mom_track_max:                    # keep the newest
            for m in sorted(self.track, key=self.track.get)[: len(self.track) - cfg.mom_track_max]:
                self.track.pop(m, None)
                self.tags.pop(m, None)
        stale = [m for m in self.track if m not in fresh]
        if stale and self.dex is not None:
            from .scout import candidate_from_pair
            try:
                best = self.dex.best_pair_per_mint(self.dex.pairs_for_mints(stale), stale)
            except Exception:
                best = {}
            for m, p in best.items():
                c = candidate_from_pair(p, sorted(self.tags.get(m, set())) + ["momentum-track"], now)
                if c.age_h > cfg.mom_max_age_h or (c.age_h > 1 and c.mcap < cfg.mom_min_mcap / 4):
                    self.track.pop(m, None)                         # aged out, or a dead launch
                    continue
                fresh[m] = c
        return list(fresh.values())

    def prefilter(self, c: Candidate) -> Kill | None:
        cfg = self.cfg
        require_stage(c, "scout", self.name)

        def kill(reason, detail, cooldown=3):
            return Kill(self.name, c.mint, c.symbol, reason, detail, cooldown_min=cooldown, book=self.book)

        age_min = c.age_h * 60
        if age_min < cfg.mom_min_age_min:
            return SKIP
        if c.age_h > cfg.mom_max_age_h:
            return kill("too_old", f"age {c.age_h:.0f}h", 24 * 60)
        if not cfg.mom_min_mcap <= c.mcap <= cfg.mom_max_mcap:
            return kill("mcap_out", f"mcap ${c.mcap:,.0f}", 5)
        from .filter import on_bonding_curve
        if not on_bonding_curve(c) and c.liquidity_usd < cfg.mom_min_liq:
            return kill("liq_low", f"liq ${c.liquidity_usd:,.0f} < ${cfg.mom_min_liq:,.0f}", 5)
        tx = c.txns.get("m5", {})
        b, s = tx.get("buys", 0), tx.get("sells", 0)
        if b + s < cfg.mom_min_txns_m5:
            return kill("quiet", f"{b + s} txns in 5m < {cfg.mom_min_txns_m5}", 2)
        if (b / s if s else float("inf")) < cfg.mom_min_buy_sell_m5:
            return kill("sellers", f"5m buy/sell {b}/{s}", 2)
        m5, h1 = c.price_change.get("m5", 0.0), c.price_change.get("h1", 0.0)
        if m5 <= 0:
            return kill("not_rising", f"{m5:+.1f}% in 5m", 2)
        if m5 > cfg.mom_max_m5_pct or h1 > cfg.mom_max_h1_pct:
            return kill("too_late", f"{m5:+.0f}% 5m / {h1:+.0f}% 1h", 10)
        if h1 <= 0:
            return kill("downtrend", f"{h1:+.1f}% in 1h", 5)
        return None

    def pick(self, v: Vetted) -> MomentumPick:
        require_stage(v, "filter", self.name)
        return MomentumPick(vetted=v, momentum=mom_quality(v.candidate, self.cfg), trail=v.trail + [self.name])
