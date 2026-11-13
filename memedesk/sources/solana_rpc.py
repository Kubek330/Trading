"""Direct Solana JSON-RPC: on-chain mint authority checks and wallet balances."""
from __future__ import annotations

import base64
import itertools
from typing import Any

from ..http import DataError, HttpClient

TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


class SolanaRPC:
    def __init__(self, http: HttpClient, url: str):
        self.http, self.url = http, url
        self._ids = itertools.count(1)

    def call(self, method: str, params: list) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        res = self.http.post(self.url, json=body)
        if "error" in res:
            raise DataError(f"RPC {method}: {res['error']}")
        return res.get("result")

    def mint_info(self, mint: str) -> dict:
        """{'mint_authority','freeze_authority','supply','decimals'} straight from chain."""
        res = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        val = (res or {}).get("value")
        if not val:
            raise DataError(f"mint account {mint} not found")
        parsed = (val.get("data") or {}).get("parsed") or {}
        if parsed.get("type") != "mint":
            raise DataError(f"{mint} is not a mint account")
        info = parsed["info"]
        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "supply": int(info.get("supply", 0)),
            "decimals": int(info.get("decimals", 0)),
        }

    # ---- wallet helpers (live mode) ----------------------------------------
    def sol_balance(self, owner: str) -> float:
        return (self.call("getBalance", [owner]) or {}).get("value", 0) / 1e9

    def token_balance_raw(self, owner: str, mint: str) -> int:
        res = self.call("getTokenAccountsByOwner",
                        [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0
        for acc in (res or {}).get("value", []):
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
            total += int(amt)
        return total

    def send_raw(self, tx_bytes: bytes) -> str:
        return self.call("sendTransaction", [base64.b64encode(tx_bytes).decode(),
                                             {"encoding": "base64", "maxRetries": 3,
                                              "preflightCommitment": "confirmed"}])

    def signature_status(self, sig: str) -> dict | None:
        res = self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        vals = (res or {}).get("value") or [None]
        return vals[0]
