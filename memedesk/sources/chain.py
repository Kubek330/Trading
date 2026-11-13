"""Read swaps straight from Solana: who bought/sold which token, when, for how much SOL.

Two backends, same output (`Swap` rows):
  - Helius (if HELIUS_API_KEY is set): RPC for signatures + the enhanced
    transactions API, which parses 100 transactions per call.
  - Public RPC: getSignaturesForAddress + getTransaction one by one, paced hard.
Parsed swaps are cached in SQLite (transactions never change), so nothing is
fetched twice, even across restarts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..http import DataError, HttpClient

log = logging.getLogger("memedesk.chain")

WSOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {WSOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


@dataclass
class Swap:
    sig: str
    slot: int
    ts: float
    wallet: str          # fee payer = the trader
    mint: str
    d_token: float       # + bought, - sold (UI units)
    d_sol: float         # SOL change of the trader incl. fees and WSOL (- spent, + received)

    @property
    def side(self) -> str:
        return "buy" if self.d_token > 0 else "sell"


def parse_rpc_tx(sig: str, tx: dict) -> list[Swap]:
    """jsonParsed getTransaction -> swaps by the fee payer (one row per traded mint)."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return []
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = msg.get("accountKeys") or []
    if not keys:
        return []
    fp = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    d_sol = ((meta.get("postBalances") or [0])[0] - (meta.get("preBalances") or [0])[0]) / 1e9
    deltas: dict[str, float] = {}
    for sign, bals in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in bals:
            if b.get("owner") != fp:
                continue
            amt = (b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0
            deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + sign * float(amt)
    d_sol += deltas.pop(WSOL, 0.0)
    out = []
    for mint, d in deltas.items():
        if mint in QUOTE_MINTS or abs(d) < 1e-12:
            continue
        out.append(Swap(sig, int(tx.get("slot") or 0), float(tx.get("blockTime") or 0), fp, mint, d, d_sol))
    return out


def parse_helius_tx(tx: dict) -> list[Swap]:
    """Helius enhanced transaction -> swaps by the fee payer."""
    if not tx or tx.get("transactionError"):
        return []
    fp = tx.get("feePayer")
    if not fp:
        return []
    deltas: dict[str, float] = {}
    for t in tx.get("tokenTransfers") or []:
        amt = float(t.get("tokenAmount") or 0)
        if t.get("toUserAccount") == fp:
            deltas[t["mint"]] = deltas.get(t["mint"], 0.0) + amt
        if t.get("fromUserAccount") == fp:
            deltas[t["mint"]] = deltas.get(t["mint"], 0.0) - amt
    d_sol = 0.0
    for a in tx.get("accountData") or []:
        if a.get("account") == fp:
            d_sol = float(a.get("nativeBalanceChange") or 0) / 1e9
    d_sol += deltas.pop(WSOL, 0.0)
    return [Swap(tx["signature"], int(tx.get("slot") or 0), float(tx.get("timestamp") or 0), fp, m, d, d_sol)
            for m, d in deltas.items() if m not in QUOTE_MINTS and abs(d) > 1e-12]


class ChainReader:
    def __init__(self, http: HttpClient, rpc_url: str, store, helius_key: str = ""):
        self.http, self.store, self.helius_key = http, store, helius_key
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}" if helius_key else rpc_url
        self._id = 0

    @property
    def backend(self) -> str:
        return "helius" if self.helius_key else "public-rpc"

    def _rpc(self, method: str, params: list):
        self._id += 1
        res = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": self._id,
                                                 "method": method, "params": params}, retries=5)
        if "error" in res:
            raise DataError(f"RPC {method}: {res['error'].get('message', res['error'])}")
        return res.get("result")

    # ---- signatures -------------------------------------------------------
    def oldest_signatures(self, address: str, n: int, max_pages: int) -> list[dict] | None:
        """The first `n` successful signatures touching `address`, oldest first.
        Returns None if the history is deeper than max_pages * 1000 (too costly to walk)."""
        before, prev, page = None, [], []
        for _ in range(max_pages):
            params = {"limit": 1000, **({"before": before} if before else {})}
            prev, page = page, (self._rpc("getSignaturesForAddress", [address, params]) or [])
            if len(page) < 1000:  # reached the account's first transaction
                newest_first = prev + page
                return [s for s in reversed(newest_first) if s.get("err") is None][:n]
            before = page[-1]["signature"]
        return None

    def recent_signatures(self, address: str, limit: int = 15) -> list[dict]:
        return self._rpc("getSignaturesForAddress", [address, {"limit": limit}]) or []

    # ---- swaps ------------------------------------------------------------
    def swaps_for(self, sigs: list[str]) -> list[Swap]:
        """Parsed swaps for the given signatures, using the cache first."""
        cached, missing = self.store.cached_swaps(sigs)
        fresh: list[Swap] = []
        if missing and self.helius_key:
            for i in range(0, len(missing), 100):
                chunk = missing[i:i + 100]
                txs = self.http.post(f"https://api.helius.xyz/v0/transactions?api-key={self.helius_key}",
                                     json={"transactions": chunk}, retries=4) or []
                for tx in txs:
                    fresh.extend(parse_helius_tx(tx))
                self.store.mark_parsed(chunk)
        elif missing:
            for sig in missing:
                try:
                    tx = self._rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                                            "maxSupportedTransactionVersion": 0}])
                except DataError as e:
                    log.debug("getTransaction %s: %s", sig[:12], e)
                    continue
                fresh.extend(parse_rpc_tx(sig, tx))
                self.store.mark_parsed([sig])
        self.store.save_swaps(fresh)
        return cached + fresh

    def wallet_recent_swaps(self, wallet: str, limit: int = 15) -> list[Swap]:
        if self.helius_key:
            txs = self.http.get(f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
                                params={"api-key": self.helius_key, "limit": limit}, retries=3) or []
            out = []
            for tx in txs:
                out.extend(parse_helius_tx(tx))
            self.store.save_swaps(out)
            self.store.mark_parsed([tx.get("signature") for tx in txs if tx.get("signature")])
            return [s for s in out if s.wallet == wallet]
        sigs = [s["signature"] for s in self.recent_signatures(wallet, limit) if s.get("err") is None]
        return [s for s in self.swaps_for(sigs) if s.wallet == wallet]
