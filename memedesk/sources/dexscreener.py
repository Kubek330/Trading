"""DexScreener: pair data (price, mcap, liquidity, age, txns, volume)."""
from __future__ import annotations

from typing import Any, Iterable

from ..http import DataError, HttpClient

QUOTES = {"SOL", "WSOL", "USDC", "USDT"}


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class DexScreener:
    def __init__(self, http: HttpClient, base: str):
        self.http, self.base = http, base.rstrip("/")

    def _mints_from(self, path: str) -> list[str]:
        data = self.http.get(f"{self.base}{path}", cache_ttl=60)
        if isinstance(data, dict):
            data = [data]
        return [d["tokenAddress"] for d in data or []
                if d.get("chainId") == "solana" and d.get("tokenAddress")]

    def discover_mints(self) -> list[str]:
        """Fresh Solana mints from the profile and boost feeds."""
        out: list[str] = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1",
                     "/token-boosts/top/v1"):
            try:
                out.extend(self._mints_from(path))
            except DataError:
                continue
        return list(dict.fromkeys(out))

    def pairs_for_mints(self, mints: Iterable[str]) -> list[dict]:
        mints = list(dict.fromkeys(mints))
        pairs: list[dict] = []
        for i in range(0, len(mints), 30):
            chunk = ",".join(mints[i:i + 30])
            data = self.http.get(f"{self.base}/tokens/v1/solana/{chunk}")
            if isinstance(data, dict):
                data = data.get("pairs") or []
            pairs.extend(p for p in data or [] if p.get("chainId") == "solana")
        return pairs

    @staticmethod
    def best_pair_per_mint(pairs: list[dict], mints: Iterable[str] | None = None) -> dict[str, dict]:
        """Deepest SOL/USD-quoted pool per base mint."""
        want = set(mints) if mints is not None else None
        best: dict[str, dict] = {}
        for p in pairs:
            base = (p.get("baseToken") or {}).get("address")
            quote = ((p.get("quoteToken") or {}).get("symbol") or "").upper()
            if not base or quote not in QUOTES or (want is not None and base not in want):
                continue
            liq = _f((p.get("liquidity") or {}).get("usd"))
            if base not in best or liq > _f((best[base].get("liquidity") or {}).get("usd")):
                best[base] = p
        return best

    def prices(self, mints: Iterable[str]) -> dict[str, dict]:
        """mint -> {'price','liq','pair'} using each mint's deepest pool."""
        best = self.best_pair_per_mint(self.pairs_for_mints(mints), mints)
        return {m: {"price": _f(p.get("priceUsd")),
                    "liq": _f((p.get("liquidity") or {}).get("usd")),
                    "mcap": _f(p.get("marketCap")) or _f(p.get("fdv")),
                    "h1": _f((p.get("priceChange") or {}).get("h1")),
                    "symbol": (p.get("baseToken") or {}).get("symbol", ""),
                    "pair": p} for m, p in best.items()}
