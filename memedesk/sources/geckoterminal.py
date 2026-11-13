"""GeckoTerminal: discovery feeds and per-pool trade tape (wallet-level)."""
from __future__ import annotations

import time
from datetime import datetime

from ..http import DataError, HttpClient

HEADERS = {"Accept": "application/json;version=20230302"}


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


class GeckoTerminal:
    def __init__(self, http: HttpClient, base: str):
        self.http, self.base = http, base.rstrip("/")

    def _pool_mints(self, path: str, params: dict | None = None) -> list[str]:
        data = self.http.get(f"{self.base}{path}", params=params, headers=HEADERS)
        mints = []
        for pool in data.get("data", []):
            tid = (((pool.get("relationships") or {}).get("base_token") or {})
                   .get("data") or {}).get("id", "")
            if tid.startswith("solana_"):
                mints.append(tid[len("solana_"):])
        return mints

    def discover_mints(self) -> list[str]:
        out: list[str] = []
        for path, params in (("/networks/solana/trending_pools", {"duration": "1h"}),
                             ("/networks/solana/trending_pools", {"duration": "6h"}),
                             ("/networks/solana/new_pools", {"page": 1})):
            try:
                out.extend(self._pool_mints(path, params))
            except DataError:
                continue
        return list(dict.fromkeys(out))

    def trades(self, pool: str, window_min: float = 60.0) -> list[dict]:
        """Last <=300 swaps on the pool inside the window:
        [{'ts','kind':'buy'|'sell','wallet','usd'}]."""
        data = self.http.get(f"{self.base}/networks/solana/pools/{pool}/trades",
                             headers=HEADERS)
        cutoff = time.time() - window_min * 60
        out = []
        for t in data.get("data", []):
            a = t.get("attributes") or {}
            ts = _ts(a.get("block_timestamp", ""))
            if ts < cutoff:
                continue
            try:
                usd = float(a.get("volume_in_usd") or 0)
            except ValueError:
                usd = 0.0
            out.append({"ts": ts, "kind": a.get("kind"),
                        "wallet": a.get("tx_from_address"), "usd": usd})
        return out
