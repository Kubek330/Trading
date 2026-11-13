"""Filter: hard kill rules on real on-chain data. Any failure kills; with
fail_closed (default) missing safety data also kills."""
from __future__ import annotations

import logging
import time

from ..config import Config
from ..http import DataError
from ..models import Candidate, Kill, SafetyFacts, Vetted, require_stage
from ..sources import rugcheck as rc

log = logging.getLogger("memedesk.filter")


class Filter:
    name = "filter"

    def __init__(self, cfg: Config, rpc, rug, bundles, logger=None, cache_ttl_s: float = 300):
        self.cfg, self.rpc, self.rug, self.bundles, self.logger = cfg, rpc, rug, bundles, logger
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, dict]] = {}

    def _report(self, mint: str) -> dict | None:
        hit = self._cache.get(mint)
        if hit and time.time() - hit[0] < self.cache_ttl_s:
            return hit[1]
        try:
            rep = self.rug.report(mint)
        except DataError as e:
            log.warning("rugcheck %s: %s", mint, e)
            return None
        self._cache[mint] = (time.time(), rep)
        holders = rep.get("totalHolders")
        if self.logger and isinstance(holders, (int, float)):
            self.logger.holder_snapshot(mint, int(holders))  # only on fresh data
        return rep

    def gather(self, c: Candidate) -> SafetyFacts:
        rep = self._report(c.mint)
        mint_auth = freeze_auth = "UNKNOWN"
        try:
            info = self.rpc.mint_info(c.mint)  # on-chain, authoritative
            mint_auth, freeze_auth = info["mint_authority"], info["freeze_authority"]
        except DataError as e:
            log.warning("rpc mint_info %s: %s", c.mint, e)
            if rep:  # fall back to RugCheck's read of the same account
                tok = rep.get("token") or {}
                mint_auth = tok.get("mintAuthority", "UNKNOWN")
                freeze_auth = tok.get("freezeAuthority", "UNKNOWN")
        bundle_pct, bundle_src = self.bundles.bundle_pct(c.mint, rep)
        holders = rep.get("totalHolders") if rep else None
        return SafetyFacts(
            mint_authority=mint_auth,
            freeze_authority=freeze_auth,
            lp_locked_pct=rc.lp_locked_pct(rep, c.pair_address) if rep else None,
            dev_pct=rc.dev_pct(rep) if rep else None,
            creator=rep.get("creator") if rep else None,
            creator_rugs=rc.creator_rug_count(rep) if rep else None,
            bundle_pct=bundle_pct,
            bundle_source=bundle_src,
            holders=int(holders) if isinstance(holders, (int, float)) else None,
            top10_pct=rc.top10_pct(rep) if rep else None,
            top_holder_owners=rc.holder_owners(rep) if rep else [],
            rugcheck_score=(rep or {}).get("score_normalised"),
        )

    def check(self, c: Candidate, f: SafetyFacts, rugged: bool = False) -> Kill | None:
        cfg = self.cfg

        def kill(reason: str, detail: str) -> Kill:
            return Kill(self.name, c.mint, c.symbol, reason, detail,
                        data={"facts": f.__dict__ if f else None})

        def unknown(what: str) -> Kill | None:
            return kill(f"unknown_{what}", f"{what} unavailable (fail-closed)") if cfg.fail_closed else None

        # 1. already pumped: cheapest check first
        h1 = c.price_change.get("h1", 0.0)
        if h1 > cfg.max_h1_change_pct:
            return Kill(self.name, c.mint, c.symbol, "pumped_1h",
                        f"+{h1:.0f}% in 1h > {cfg.max_h1_change_pct:.0f}%", cooldown_min=60)
        # 2. liquidity depth versus mcap
        if c.liq_mcap_pct < cfg.min_liq_mcap_pct:
            return kill("liq_thin", f"liq/mcap {c.liq_mcap_pct:.1f}% < {cfg.min_liq_mcap_pct:.0f}%")
        if rugged:
            return kill("rugged", "RugCheck marks token as rugged")
        # 3. authorities
        if f.mint_authority == "UNKNOWN":
            if k := unknown("mint_authority"):
                return k
        elif f.mint_authority:
            return kill("mint_authority_live", f"mint authority {f.mint_authority[:8]}… not revoked")
        if f.freeze_authority == "UNKNOWN":
            if k := unknown("freeze_authority"):
                return k
        elif f.freeze_authority:
            return kill("freeze_authority_live", f"freeze authority {f.freeze_authority[:8]}… not revoked")
        # 4. LP burned or locked
        if f.lp_locked_pct is None:
            if k := unknown("lp_lock"):
                return k
        elif f.lp_locked_pct < cfg.min_lp_locked_pct:
            return kill("lp_unlocked", f"LP locked/burned {f.lp_locked_pct:.1f}% < {cfg.min_lp_locked_pct:.0f}%")
        # 5. dev holdings / history
        if f.dev_pct is None:
            if k := unknown("dev_holdings"):
                return k
        elif f.dev_pct > cfg.max_dev_pct:
            return kill("dev_heavy", f"dev holds {f.dev_pct:.1f}% > {cfg.max_dev_pct:.0f}%")
        if f.creator_rugs is None:
            if k := unknown("dev_history"):
                return k
        elif f.creator_rugs > 0:
            return kill("dev_past_rugs", f"creator {str(f.creator)[:8]}… tied to {f.creator_rugs} rug(s)")
        if self.logger and f.creator and self.logger.get(f"rugger:{f.creator}"):
            return kill("dev_past_rugs", "creator on local rugger blacklist")
        # 6. concentration: top-10 holders, pools excluded
        if f.top10_pct is None:
            if k := unknown("top10"):
                return k
        elif f.top10_pct > cfg.max_top10_pct:
            return kill("top10_heavy", f"top-10 holders own {f.top10_pct:.1f}% > {cfg.max_top10_pct:.0f}%")
        # 7. bundled launch
        if f.bundle_pct is None:
            if k := unknown("bundle"):
                return k
        elif f.bundle_pct > cfg.max_bundle_pct:
            return kill("bundled_launch", f"{f.bundle_pct:.1f}% bundled > {cfg.max_bundle_pct:.0f}% ({f.bundle_source})")
        return None

    def run(self, c: Candidate) -> Vetted | Kill:
        require_stage(c, ("scout", "whale_scout"), self.name)
        h1 = c.price_change.get("h1", 0.0)
        if h1 > self.cfg.max_h1_change_pct or c.liq_mcap_pct < self.cfg.min_liq_mcap_pct:
            # market-data kills need no on-chain calls
            return self.check(c, SafetyFacts(*([None] * 9)))
        facts = self.gather(c)
        rep = self._cache.get(c.mint, (0, {}))[1]
        k = self.check(c, facts, rugged=bool(rep.get("rugged")))
        if k:
            return k
        return Vetted(candidate=c, facts=facts, trail=c.trail + [self.name])
