"""Jupiter Tokens API v2: organicScore (0-100) and organicScoreLabel (high/medium/low).

Jupiter's estimate of how much of a token's trading comes from real wallets
rather than wash trading, bots or a coordinated pump. One call covers up to
100 mints; results are cached for 5 minutes.

With JUP_API_KEY (free at portal.jup.ag) it calls api.jup.ag with the x-api-key
header; without one it tries the keyless lite-api.jup.ag host.
"""
from __future__ import annotations

import threading
import time

from ..http import DataError, HttpClient

TTL_S = 300.0


class JupiterTokens:
    def __init__(self, http: HttpClient, api_key: str = ""):
        self.http, self.key = http, api_key
        self.base = "https://api.jup.ag/tokens/v2" if api_key else "https://lite-api.jup.ag/tokens/v2"
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    def prefetch(self, mints: list[str]) -> None:
        now = time.time()
        with self._lock:
            need = [m for m in dict.fromkeys(mints) if self._cache.get(m, (0, None))[0] < now]
        for i in range(0, len(need), 100):
            chunk = need[i:i + 100]
            data = self.http.get(f"{self.base}/search", params={"query": ",".join(chunk)},
                                 headers={"x-api-key": self.key} if self.key else None, retries=3)
            if isinstance(data, dict):
                data = data.get("tokens") or data.get("data") or []
            found = {t.get("id"): t for t in data or [] if isinstance(t, dict)}
            with self._lock:
                for m in chunk:  # a mint Jupiter doesn't know is cached as None (unknown)
                    t = found.get(m)
                    self._cache[m] = (now + TTL_S, None if t is None else {
                        "score": t.get("organicScore"), "label": t.get("organicScoreLabel"),
                        "verified": t.get("isVerified")})

    def organic(self, mint: str) -> dict | None:
        """{'score','label','verified'} or None if unknown/unavailable."""
        with self._lock:
            hit = self._cache.get(mint)
        if hit and hit[0] >= time.time():
            return hit[1]
        try:
            self.prefetch([mint])
        except DataError:
            return None
        with self._lock:
            return self._cache.get(mint, (0, None))[1]
