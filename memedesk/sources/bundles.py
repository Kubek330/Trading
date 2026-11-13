"""Launch-bundle detection. Primary: trench.bot bundle analysis (share of supply
bought in same-slot bundles at launch). Fallback: RugCheck insider holdings."""
from __future__ import annotations

from ..http import DataError, HttpClient
from . import rugcheck


class BundleChecker:
    def __init__(self, http: HttpClient, base: str):
        self.http, self.base = http, base.rstrip("/")

    def bundle_pct(self, mint: str, rug_report: dict | None) -> tuple[float | None, str | None]:
        try:
            # launch bundling is history: cache it for an hour
            d = self.http.get(f"{self.base}/bundle/bundle_advanced/{mint}", retries=2,
                              cache_ttl=3600)
            for key in ("total_percentage_bundled", "total_holding_percentage"):
                v = d.get(key)
                if v is not None:
                    return float(v), f"trench.{key}"
        except (DataError, TypeError, ValueError, AttributeError):
            pass
        if rug_report:
            v = rugcheck.insider_pct(rug_report)
            if v is not None:
                return v, "rugcheck.insiders"
        return None, None
