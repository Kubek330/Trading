"""AI book: an LLM decides on its own what to buy, how much, and when to sell.

No filter blocks it: it gets every number the desk has about each coin, safety facts included,
as information, and makes the call itself. Code sizes (2-4% of the book from the safety facts),
simulates fills, and enforces the exit plan the AI gives with every buy (stop, take-profit, time
limit), because the AI only looks every few minutes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..config import Config
from ..models import Candidate, SafetyFacts, Vetted

log = logging.getLogger("memedesk.ai")

SYSTEM = """You are the trader of a paper-trading Solana memecoin book with $1000.
Every few minutes you get candidate tokens (live DEX data plus on-chain safety facts), your open
positions and your most recent closed trades. Decide yourself. Nothing blocks you: if a token looks like
a scam (mint/freeze authority live, LP not burned or locked, a few wallets holding most of the supply,
dev holding a lot, dangerous Token-2022 extensions, fake-looking volume), it is your job to skip it.
Most new memecoins go to zero, so only buy when you see a real edge; skipping everything is a valid
answer. Learn from your recent trades: do not repeat what lost money.
Costs: ~1-3% price impact + fees each way. Position size is set by the desk (2-4% of the book), not you.
Every buy needs an exit plan the desk enforces between your calls:
  stop_pct 5-25 (sell all at -stop_pct%), tp_pct 10-200 (sell all at +tp_pct%),
  max_hold_min 15-360 (sell when the idea has not worked by then).
For open positions decide hold or sell (sell takes the whole position).
Reply with JSON only:
{"buys":[{"mint":"...","stop_pct":15,"tp_pct":40,"max_hold_min":90,"reason":"<= 20 words"}],
 "sells":[{"id":"...","reason":"<= 20 words"}]}"""


def _clamp(x, lo, hi, default):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v)) if v == v else default


def code_size_pct(f: SafetyFacts | None) -> float:
    """The desk, not the model, sizes: 4% when the on-chain facts are clean, 2% when anything is
    unknown or looks risky, 3% otherwise."""
    if f is None:
        return 2.0
    red = (f.mint_authority not in (None,) or f.freeze_authority not in (None,)
           or bool(getattr(f, "jupiter_sus", False)) or bool(getattr(f, "token2022_risks", None))
           or (f.top10_pct or 0) > 40 or (f.dev_pct or 0) > 10 or (f.creator_rugs or 0) > 0)
    if red or f.top10_pct is None or f.dev_pct is None:
        return 2.0
    clean = (f.lp_locked_pct is None or f.lp_locked_pct >= 90) and f.top10_pct <= 25 and f.dev_pct <= 5
    return 4.0 if clean else 3.0


@dataclass
class AIPick:
    vetted: Vetted
    size_pct: float
    reason: str
    trail: list[str] = field(default_factory=list)
    book: str = "ai"
    score: float | None = None
    momentum: float = 0.0
    stop_pct: float = 15.0
    tp_pct: float = 40.0
    max_hold_min: float = 120.0


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
            "jupiter_flags_suspicious": bool(getattr(f, "jupiter_sus", False)),
            "token2022_dangerous_extensions": list(getattr(f, "token2022_risks", None) or [])}
    return d


class AITrader:
    name = "ai"
    book = "ai"

    def __init__(self, cfg: Config, llm):
        self.cfg, self.llm = cfg, llm
        self.last_error = ""
        self.backoff_until = 0.0
        self._strikes = 0

    @property
    def enabled(self) -> bool:
        return bool(self.llm and self.llm.enabled)

    def rate_limited(self, now: float) -> bool:
        return now < self.backoff_until

    def note_error(self, err: str, now: float) -> float:
        """429 / quota: wait 5, 10, 20 ... up to 60 min before the next call. Returns the wait in seconds."""
        self.last_error = err[:200]
        if "429" not in err and "quota" not in err.lower() and "RESOURCE_EXHAUSTED" not in err:
            return 0.0
        self._strikes += 1
        wait = min(3600.0, 300.0 * 2 ** (self._strikes - 1))
        self.backoff_until = now + wait
        return wait

    def decide(self, cands: list[dict], positions: list[dict], history: list[dict] | None = None) -> dict:
        prompt = json.dumps({"candidates": cands, "open_positions": positions,
                             "recent_closed_trades": history or []}, default=str)
        out = self.llm.json(SYSTEM, prompt)
        self._strikes = 0
        if not isinstance(out, dict):
            return {"buys": [], "sells": []}
        buys = [b for b in out.get("buys") or [] if isinstance(b, dict) and b.get("mint")]
        sells = [s for s in out.get("sells") or [] if isinstance(s, dict) and s.get("id")]
        return {"buys": buys, "sells": sells}

    def pick(self, c: Candidate, f: SafetyFacts, plan: dict | None = None, reason: str = "") -> AIPick:
        plan = plan if isinstance(plan, dict) else {}
        v = Vetted(candidate=c, facts=f, trail=[*c.trail, "ai"])
        return AIPick(vetted=v, size_pct=code_size_pct(f), reason=str(reason or plan.get("reason") or "")[:160],
                      trail=[*c.trail, "ai"],
                      stop_pct=_clamp(plan.get("stop_pct"), 5, 25, 15.0),
                      tp_pct=_clamp(plan.get("tp_pct"), 10, 200, 40.0),
                      max_hold_min=_clamp(plan.get("max_hold_min"), 15, 360, 120.0))
