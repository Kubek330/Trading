"""GeckoTerminal: discovery feeds, candles and per-pool trade tape (wallet-level).

Primary: the free public GeckoTerminal API (no key, no monthly cap).
Backup: if COINGECKO_API_KEY (Demo) is set, a request that the public API
rate-limits (429) is retried once through CoinGecko's onchain API, which serves
the same data. The Demo plan has a hard monthly cap (10,000 calls), so the
backup draws from a persisted monthly budget and stops at DESK_COINGECKO_MONTHLY_CAP.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from ..http import DataError, HttpClient

HEADERS = {"Accept": "application/json;version=20230302"}


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


CG_ONCHAIN = "https://api.coingecko.com/api/v3/onchain"


class MonthlyBudget:
    """Counts calls per calendar month (UTC) in the desk's account table."""

    def __init__(self, store, name: str, cap: int):
        self.store, self.name, self.cap = store, name, cap
        self._lock = threading.Lock()

    def _key(self) -> str:
        return f"budget:{self.name}:{time.strftime('%Y-%m', time.gmtime())}"

    @property
    def used(self) -> int:
        return int(self.store.get(self._key(), 0) or 0)

    def take(self) -> bool:
        with self._lock:
            used = self.used
            if used >= self.cap:
                return False
            self.store.put(self._key(), used + 1)
            return True


class GeckoTerminal:
    def __init__(self, http: HttpClient, base: str, cg_key: str = "", budget: MonthlyBudget | None = None):
        self.http, self.base = http, base.rstrip("/")
        self.cg_key, self.budget = cg_key, budget
        self.fallback_calls = 0

    def _get(self, path: str, params: dict | None = None, cache_ttl: float = 0.0):
        try:
            return self.http.get(f"{self.base}{path}", params=params, headers=HEADERS, cache_ttl=cache_ttl,
                                 retries=2 if self.cg_key else 4)
        except DataError as e:
            if not (self.cg_key and "429" in str(e)) or "shutting down" in str(e):
                raise
            if self.budget is not None and not self.budget.take():
                raise DataError(f"{e} (CoinGecko Demo monthly budget used up)") from None
            self.fallback_calls += 1
            return self.http.get(f"{CG_ONCHAIN}{path}", params=params, cache_ttl=cache_ttl, retries=2,
                                 headers={"x-cg-demo-api-key": self.cg_key, "accept": "application/json"})

    def _pool_mints(self, path: str, params: dict | None = None) -> list[str]:
        data = self._get(path, params, cache_ttl=120)
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

    def discover_pools(self) -> list[dict]:
        """Pools with real 24h/6h action: [{'pool','mint','symbol'}] (deduped by mint)."""
        out: dict[str, dict] = {}
        for path, params in (("/networks/solana/trending_pools", {"duration": "24h"}),
                             ("/networks/solana/trending_pools", {"duration": "6h"}),
                             ("/networks/solana/pools", {"sort": "h24_volume_usd_desc"}),
                             ("/networks/solana/pools", {"sort": "h24_tx_count_desc"})):
            try:
                data = self._get(path, params, cache_ttl=600)
            except DataError:
                continue
            for pool in data.get("data", []):
                a = pool.get("attributes") or {}
                tid = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if not tid.startswith("solana_") or not a.get("address"):
                    continue
                mint = tid[len("solana_"):]
                out.setdefault(mint, {"pool": a["address"], "mint": mint,
                                      "symbol": (a.get("name") or "?").split(" /")[0].strip()})
        return list(out.values())

    def ohlcv(self, pool: str, timeframe: str = "hour", aggregate: int = 4, limit: int = 42) -> list[list[float]]:
        """[[ts, open, high, low, close, volume], ...] oldest first."""
        data = self._get(f"/networks/solana/pools/{pool}/ohlcv/{timeframe}",
                         {"aggregate": aggregate, "limit": limit, "currency": "usd"}, cache_ttl=1800)
        rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        return sorted(([float(x) for x in r[:6]] for r in rows if len(r) >= 6), key=lambda r: r[0])

    def trades(self, pool: str, window_min: float = 60.0) -> list[dict]:
        """Last <=300 swaps on the pool inside the window:
        [{'ts','kind':'buy'|'sell','wallet','usd'}]."""
        data = self._get(f"/networks/solana/pools/{pool}/trades", cache_ttl=60)
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
