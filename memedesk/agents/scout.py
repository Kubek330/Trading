"""Scout: find Solana pairs 30m-72h old with mcap $300k-$20M on DexScreener."""
from __future__ import annotations

import logging
import time

from ..config import Config
from ..http import DataError
from ..models import Candidate, Kill
from ..sources.dexscreener import DexScreener, _f

log = logging.getLogger("memedesk.scout")


def candidate_from_pair(p: dict, sources: list[str], now: float | None = None) -> Candidate:
    base = p.get("baseToken") or {}
    txns = {k: {"buys": int((v or {}).get("buys", 0)), "sells": int((v or {}).get("sells", 0))}
            for k, v in (p.get("txns") or {}).items()}
    return Candidate(
        mint=base.get("address", ""),
        symbol=base.get("symbol", "?"),
        name=base.get("name", ""),
        pair_address=p.get("pairAddress", ""),
        dex_id=p.get("dexId", ""),
        quote_symbol=(p.get("quoteToken") or {}).get("symbol", ""),
        price_usd=_f(p.get("priceUsd")),
        mcap=_f(p.get("marketCap")) or _f(p.get("fdv")),
        liquidity_usd=_f((p.get("liquidity") or {}).get("usd")),
        pair_created_ms=int(p.get("pairCreatedAt") or 0),
        price_change={k: _f(v) for k, v in (p.get("priceChange") or {}).items()},
        txns=txns,
        volume={k: _f(v) for k, v in (p.get("volume") or {}).items()},
        sources=sources,
        seen_ts=now or time.time(),
        image=str(((p.get("info") or {}).get("imageUrl")) or ""),
    )


class Scout:
    name = "scout"

    def __init__(self, cfg: Config, dex: DexScreener, gecko=None, logger=None):
        self.cfg, self.dex, self.gecko, self.logger = cfg, dex, gecko, logger

    def discover(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        try:
            for m in self.dex.discover_mints():
                found.setdefault(m, []).append("dexscreener")
        except DataError as e:
            log.warning("dexscreener discovery failed: %s", e)
        if self.gecko and self.cfg.use_geckoterminal_discovery:
            try:
                for m in self.gecko.discover_mints():
                    found.setdefault(m, []).append("geckoterminal")
            except DataError as e:
                log.warning("geckoterminal discovery failed: %s", e)
        return found

    def gate(self, c: Candidate) -> Kill | None:
        cfg = self.cfg
        if not c.mint or not c.pair_created_ms or c.price_usd <= 0:
            return Kill(self.name, c.mint, c.symbol, "bad_pair_data", "missing price/age")
        age_min = c.age_h * 60
        if age_min < cfg.min_age_min:
            # recheck once it is old enough
            return Kill(self.name, c.mint, c.symbol, "too_young", f"age {age_min:.0f}m",
                        cooldown_min=max(1.0, cfg.min_age_min - age_min))
        if c.age_h > cfg.max_age_h:
            return Kill(self.name, c.mint, c.symbol, "too_old", f"age {c.age_h:.1f}h",
                        cooldown_min=24 * 60)
        if c.mcap < cfg.min_mcap:
            return Kill(self.name, c.mint, c.symbol, "mcap_low", f"mcap ${c.mcap:,.0f}")
        if c.mcap > cfg.max_mcap:
            return Kill(self.name, c.mint, c.symbol, "mcap_high", f"mcap ${c.mcap:,.0f}")
        return None

    def run(self, skip=lambda mint: False) -> tuple[list[Candidate], list[Kill]]:
        found = {m: s for m, s in self.discover().items() if not skip(m)}
        if not found:
            return [], []
        try:
            best = self.dex.best_pair_per_mint(self.dex.pairs_for_mints(found), found)
        except DataError as e:
            log.warning("dexscreener pairs failed: %s", e)
            return [], []
        now = time.time()
        passed, kills = [], []
        for mint, pair in best.items():
            c = candidate_from_pair(pair, found[mint], now)
            k = self.gate(c)
            if k:
                kills.append(k)
            else:
                passed.append(c)
        # freshest liquidity-rich pairs first; cap per cycle for rate limits
        passed.sort(key=lambda c: (c.volume.get("h1", 0)), reverse=True)
        return passed[: self.cfg.scout_max_candidates], kills
