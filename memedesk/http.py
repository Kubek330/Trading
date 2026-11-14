"""Stdlib-only JSON HTTP client (runs on Termux with no compiled packages).

- per-host request pacing (requests/minute)
- exponential backoff with jitter on 429/5xx/network errors, honouring Retry-After
- a 429 puts the whole host in a penalty box, so other threads back off too
- optional TTL cache for GETs
"""
from __future__ import annotations

import json as _json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode, urlparse

log = logging.getLogger("memedesk.http")

DEFAULT_RPM = {
    "api.dexscreener.com": 240,
    "api.geckoterminal.com": 25,
    "api.rugcheck.xyz": 50,
    "trench.bot": 25,
    "api.mainnet-beta.solana.com": 40,
    "api.x.ai": 30,
    "api.telegram.org": 20,
}
UA = "Mozilla/5.0 (Linux; Android) memedesk/2.0"


class DataError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, timeout: float = 20.0, rpm: dict[str, int] | None = None,
                 max_backoff_s: float = 90.0):
        self.timeout = timeout
        self.rpm = {**DEFAULT_RPM, **(rpm or {})}
        self.max_backoff_s = max_backoff_s
        self._next_slot: dict[str, float] = {}
        self._penalty: dict[str, float] = {}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.stats = {"requests": 0, "errors": 0, "rate_limited": 0, "cache_hits": 0}
        self.limited_by_host: dict[str, int] = {}
        self.stop_event = threading.Event()  # set on shutdown: aborts waits immediately

    def _sleep(self, secs: float) -> None:
        if secs > 0 and self.stop_event.wait(secs):
            raise DataError("desk shutting down")

    def _wait_turn(self, host: str) -> None:
        gap = 60.0 / self.rpm.get(host, 60)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, now), self._penalty.get(host, 0.0))
            self._next_slot[host] = slot + gap
        self._sleep(slot - time.monotonic())

    def _backoff(self, host: str, attempt: int, retry_after: str | None, penalize: bool) -> None:
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            delay = float(retry_after)
        else:
            delay = min(self.max_backoff_s, 2.0 * 2 ** attempt) * (0.7 + 0.6 * random.random())
        if penalize:
            with self._lock:
                self._penalty[host] = max(self._penalty.get(host, 0.0), time.monotonic() + delay)
        self._sleep(delay)

    def request(self, method: str, url: str, *, params: dict | None = None, json: Any = None,
                headers: dict | None = None, retries: int = 4, cache_ttl: float = 0.0) -> Any:
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        key = f"{method} {url} {_json.dumps(json, sort_keys=True) if json is not None else ''}"
        if cache_ttl > 0:
            hit = self._cache.get(key)
            if hit and hit[0] > time.time():
                self.stats["cache_hits"] += 1
                return hit[1]
        host = urlparse(url).hostname or ""
        hdrs = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
        body = None
        if json is not None:
            body = _json.dumps(json).encode()
            hdrs["Content-Type"] = "application/json"
        last = ""
        for attempt in range(retries):
            if self.stop_event.is_set():
                raise DataError("desk shutting down")
            self._wait_turn(host)
            self.stats["requests"] += 1
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    self.stats["rate_limited" if e.code == 429 else "errors"] += 1
                    if e.code == 429:
                        self.limited_by_host[host] = self.limited_by_host.get(host, 0) + 1
                    last = f"HTTP {e.code}"
                    self._backoff(host, attempt, e.headers.get("Retry-After"), penalize=e.code == 429)
                    continue
                self.stats["errors"] += 1
                detail = e.read()[:200].decode(errors="replace")
                raise DataError(f"HTTP {e.code} from {host}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                self.stats["errors"] += 1
                last = f"{type(e).__name__}: {e}"
                self._backoff(host, attempt, None, penalize=False)
                continue
            try:
                data = _json.loads(raw)
            except ValueError:
                raise DataError(f"non-JSON from {host}") from None
            if cache_ttl > 0:
                self._cache[key] = (time.time() + cache_ttl, data)
                if len(self._cache) > 5000:
                    now = time.time()
                    self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            return data
        raise DataError(f"{method} {host} failed after {retries} tries: {last}")

    def get(self, url: str, **kw) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Any:
        return self.request("POST", url, **kw)
