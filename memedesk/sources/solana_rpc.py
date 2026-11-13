"""Solana JSON-RPC (read-only): on-chain mint/freeze authority checks.

Built for the free public endpoint: paced by HttpClient, retried with backoff,
and cached. A revoked authority can never come back, so fully revoked mints
are cached for the life of the process; anything else is re-read after 10 min.
"""
from __future__ import annotations

import itertools
import threading
import time
from typing import Any

from ..http import DataError, HttpClient

LIVE_TTL_S = 600.0


class SolanaRPC:
    def __init__(self, http: HttpClient, url: str):
        self.http, self.url = http, url
        self._ids = itertools.count(1)
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def call(self, method: str, params: list) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        res = self.http.post(self.url, json=body, retries=5)
        if "error" in res:
            raise DataError(f"RPC {method}: {res['error']}")
        return res.get("result")

    def mint_info(self, mint: str) -> dict:
        with self._lock:
            hit = self._cache.get(mint)
        if hit and hit[0] > time.time():
            return hit[1]
        res = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        val = (res or {}).get("value")
        if not val:
            raise DataError(f"mint account {mint} not found")
        parsed = (val.get("data") or {}).get("parsed") or {}
        if parsed.get("type") != "mint":
            raise DataError(f"{mint} is not a mint account")
        info = parsed["info"]
        out = {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "supply": int(info.get("supply", 0)),
            "decimals": int(info.get("decimals", 0)),
        }
        revoked = out["mint_authority"] is None and out["freeze_authority"] is None
        with self._lock:
            self._cache[mint] = (float("inf") if revoked else time.time() + LIVE_TTL_S, out)
        return out
