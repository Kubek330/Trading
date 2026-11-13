"""RugCheck token report: LP lock/burn, creator, holders, insider graph, rug history."""
from __future__ import annotations

from typing import Any

from ..http import HttpClient


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class RugCheck:
    def __init__(self, http: HttpClient, base: str):
        self.http, self.base = http, base.rstrip("/")

    def report(self, mint: str) -> dict:
        return self.http.get(f"{self.base}/tokens/{mint}/report", retries=3)


def lp_locked_pct(report: dict, pair_address: str | None = None) -> float | None:
    """LP locked+burned % for the traded pool (falls back to the deepest market)."""
    markets = report.get("markets") or []
    if not markets:
        return None
    chosen = None
    if pair_address:
        chosen = next((m for m in markets if m.get("pubkey") == pair_address), None)
    if chosen is None:
        chosen = max(markets, key=lambda m: _num((m.get("lp") or {}).get("quoteUSD")) or 0)
    return _num((chosen.get("lp") or {}).get("lpLockedPct"))


def dev_pct(report: dict) -> float | None:
    creator = report.get("creator")
    token = report.get("token") or {}
    supply = _num(token.get("supply"))
    pct = None
    bal = _num(report.get("creatorBalance"))
    if bal is not None and supply:
        pct = 100.0 * bal / supply
    for h in report.get("topHolders") or []:
        if creator and h.get("owner") == creator:
            hp = _num(h.get("pct"))
            if hp is not None:
                pct = max(pct or 0.0, hp)
    if pct is None and creator:
        pct = 0.0  # creator known and absent from holders/balance
    return pct


def creator_rug_count(report: dict) -> int | None:
    """Count of prior rugs tied to the creator, from RugCheck's risk list and
    creator token history. None if RugCheck didn't return a creator at all."""
    if not report.get("creator"):
        return None
    n = 0
    for r in report.get("risks") or []:
        name = (r.get("name") or "").lower()
        desc = (r.get("description") or "").lower()
        if "rug" in name or ("creator" in name and "rug" in desc):
            n += 1
    for t in report.get("creatorTokens") or []:
        if t.get("rugged") or t.get("isRugged"):
            n += 1
    return n


def insider_pct(report: dict) -> float | None:
    """% of supply held by wallets RugCheck flags as insiders (bundle proxy)."""
    holders = report.get("topHolders")
    if holders is None:
        return None
    return sum(_num(h.get("pct")) or 0.0 for h in holders if h.get("insider"))


def holder_owners(report: dict) -> list[str]:
    return [h["owner"] for h in report.get("topHolders") or [] if h.get("owner")]


# Account types that hold tokens on behalf of a pool, not a person.
_POOL_TYPES = {"AMM", "AMM_POOL", "POOL", "BONDING_CURVE", "LP", "LIQUIDITY_POOL"}


def _pool_accounts(report: dict) -> set[str]:
    out: set[str] = set()
    for m in report.get("markets") or []:
        for k in ("pubkey", "liquidityAAccount", "liquidityBAccount", "mintLPAccount"):
            v = m.get(k)
            if isinstance(v, str):
                out.add(v)
            elif isinstance(v, dict) and v.get("pubkey"):
                out.add(v["pubkey"])
    for addr, meta in (report.get("knownAccounts") or {}).items():
        if str((meta or {}).get("type", "")).upper() in _POOL_TYPES:
            out.add(addr)
    return out


def top10_pct(report: dict) -> float | None:
    """% of supply held by the 10 largest holders, excluding pool/AMM accounts."""
    holders = report.get("topHolders")
    if not holders:
        return None
    pools = _pool_accounts(report)
    people = [h for h in holders
              if h.get("address") not in pools and h.get("owner") not in pools]
    pcts = sorted((_num(h.get("pct")) or 0.0 for h in people), reverse=True)
    return sum(pcts[:10])
