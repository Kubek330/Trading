"""AI book: an LLM decides on its own what to buy, how much, and when to sell.

No filter blocks it: it gets every number the desk has about each coin, safety facts included,
as information, and makes the call itself. Code only sizes (<= 8% of the book), simulates fills,
and keeps an emergency stop + max hold because the AI looks once a minute.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..config import Config
from ..models import Candidate, SafetyFacts, Vetted

log = logging.getLogger("memedesk.ai")

SYSTEM = """You are the trader of a paper-trading Solana memecoin book with $1000.
Each minute you get candidate tokens (live DEX data plus on-chain safety facts) and your open positions.
Decide yourself. Nothing blocks you: if a token looks like a scam (mint/freeze authority live, LP not
burned or locked, a few wallets holding most of the supply, dev holding a lot, fake-looking volume),
it is your job to notice and skip it. Most new memecoins go to zero, so only buy when you see a real
edge; skipping everything is a valid answer. You pay ~2.5% slippage+fees each way.
For open positions decide hold or sell (sell takes the whole position).
Reply with JSON only:
{"buys":[{"mint":"...","size_pct":1-8,"reason":"<= 20 words"}],
 "sells":[{"id":"...","reason":"<= 20 words"}]}"""


@dataclass
class AIPick:
    vetted: Vetted
    size_pct: float
    reason: str
    trail: list[str] = field(default_factory=list)
    book: str = "ai"
    score: float | None = None
    momentum: float = 0.0


def candidate_payload(c: Candidate, f: SafetyFacts | None) -> dict:
    tx = {k: v for k, v in c.txns.items() if k in ("m5", "h1", "h6")}
    d = {"mint": c.mint, "symbol": c.symbol, "age_min": round(c.age_h * 60), "mcap_usd": round(c.mcap),
         "liquidity_usd": round(c.liquidity_usd), "dex": c.dex_id,
         "price_change_pct": {k: c.price_change.get(k) for k in ("m5", "h1", "h6", "h24")},
         "txns": tx, "volume_usd": {k: round(v) for k, v in c.volume.items() if k in ("m5", "h1", "h6")},
         "seen_on": sorted(set(c.sources or []))}
    if f is not None:
        d["safety"] = {
            "mint_authority_revoked": None if f.mint_authority == "UNKNOWN" else f.mint_authority is None,
            "freeze_authority_revoked": None if f.freeze_authority == "UNKNOWN" else f.freeze_authority is None,
            "lp_locked_or_burned_pct": f.lp_locked_pct, "top10_holders_pct": f.top10_pct,
            "dev_holds_pct": f.dev_pct, "creator_past_rugs": f.creator_rugs, "bundled_launch_pct": f.bundle_pct,
            "holders": f.holders, "jupiter_organic_score": f.organic_score,
            "jupiter_flags_suspicious": bool(getattr(f, "jupiter_sus", False))}
    return d


class AITrader:
    name = "ai"
    book = "ai"

    def __init__(self, cfg: Config, llm):
        self.cfg, self.llm = cfg, llm
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.llm and self.llm.enabled)

    def decide(self, cands: list[dict], positions: list[dict]) -> dict:
        prompt = json.dumps({"candidates": cands, "open_positions": positions}, default=str)
        out = self.llm.json(SYSTEM, prompt)
        if not isinstance(out, dict):
            return {"buys": [], "sells": []}
        buys = [b for b in out.get("buys") or [] if isinstance(b, dict) and b.get("mint")]
        sells = [s for s in out.get("sells") or [] if isinstance(s, dict) and s.get("id")]
        return {"buys": buys, "sells": sells}

    def pick(self, c: Candidate, f: SafetyFacts, size_pct, reason: str) -> AIPick:
        try:
            size = max(1.0, min(8.0, float(size_pct)))
        except (TypeError, ValueError):
            size = 3.0
        v = Vetted(candidate=c, facts=f, trail=[*c.trail, "ai"])
        return AIPick(vetted=v, size_pct=size, reason=str(reason or "")[:160], trail=[*c.trail, "ai"])
