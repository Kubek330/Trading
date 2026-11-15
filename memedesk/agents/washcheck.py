"""Fake-volume (wash trading) detection from the wallet-level trade tape.

Wash trading shows up as the same few wallets generating most of the trades and
volume, often buying and selling back and forth. Checked on the last <=300 swaps.
"""
from __future__ import annotations

from collections import defaultdict

from ..config import Config


def wash_reason(trades: list[dict], cfg: Config) -> str | None:
    """None if the tape looks organic (or is too short to judge), else why it looks fake."""
    trades = [t for t in trades if t.get("wallet")]
    n = len(trades)
    if n < cfg.wash_min_trades:
        return None
    vol: dict[str, float] = defaultdict(float)
    buys: dict[str, int] = defaultdict(int)
    sells: dict[str, int] = defaultdict(int)
    for t in trades:
        vol[t["wallet"]] += float(t.get("usd") or 0)
        (buys if t.get("kind") == "buy" else sells)[t["wallet"]] += 1
    total = sum(vol.values()) or 1e-9
    uniq = len(vol)
    if uniq / n < cfg.wash_min_unique_ratio:
        return f"{n} trades from only {uniq} wallets"
    top3 = sum(sorted(vol.values(), reverse=True)[:3]) / total
    if top3 > cfg.wash_max_top3_share:
        return f"top 3 wallets = {top3:.0%} of volume"
    loop = sum(v for w, v in vol.items() if buys[w] >= 2 and sells[w] >= 2) / total
    if loop > cfg.wash_max_roundtrip_share:
        return f"{loop:.0%} of volume from wallets buying and selling back and forth"
    return None
