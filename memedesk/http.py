"""Small JSON HTTP client with per-host rate limiting and retries."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("memedesk.http")

# requests per minute, conservative versus each API's published limit
DEFAULT_RPM = {
    "api.dexscreener.com": 250,
    "api.geckoterminal.com": 25,
    "api.rugcheck.xyz": 60,
    "trench.bot": 30,
    "api.mainnet-beta.solana.com": 80,
    "lite-api.jup.ag": 50,
    "api.x.ai": 30,
    "api.telegram.org": 20,
}


class DataError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, timeout: float = 15.0, rpm: dict[str, int] | None = None):
        self.timeout = timeout
        self.rpm = {**DEFAULT_RPM, **(rpm or {})}
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "memedesk/1.0"

    def _throttle(self, host: str) -> None:
        rpm = self.rpm.get(host, 60)
        gap = 60.0 / rpm
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = slot + gap
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, url: str, *, params=None, json=None,
                headers=None, retries: int = 3) -> Any:
        host = urlparse(url).hostname or ""
        last: Exception | None = None
        for attempt in range(retries):
            self._throttle(host)
            try:
                r = self.session.request(method, url, params=params, json=json,
                                         headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = DataError(f"{r.status_code} from {host}")
                retry_after = r.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit()
                           else 2.0 * (attempt + 1))
                continue
            if r.status_code == 404:
                raise DataError(f"404 from {url}")
            if r.status_code >= 400:
                raise DataError(f"{r.status_code} from {url}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError as e:
                raise DataError(f"non-JSON from {url}") from e
        raise DataError(f"{method} {url} failed after {retries} tries: {last}")

    def get(self, url: str, **kw) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Any:
        return self.request("POST", url, **kw)
