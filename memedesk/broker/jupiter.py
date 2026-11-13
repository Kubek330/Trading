"""Live broker: Jupiter swap API, signed locally, sent over Solana RPC.

Only constructed when DESK_MODE=live and DESK_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY.
Needs `pip install solders`.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

from ..http import DataError, HttpClient
from ..models import Fill, Position

log = logging.getLogger("memedesk.live")
SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterBroker:
    mode = "live"

    def __init__(self, http: HttpClient, jup_url: str, rpc, dex, keypair_path: str,
                 slippage_bps: int, sol_reserve: float):
        from solders.keypair import Keypair  # lazy: paper mode doesn't need solders

        secret = json.loads(Path(keypair_path).expanduser().read_text())
        self.kp = Keypair.from_bytes(bytes(secret))
        self.owner = str(self.kp.pubkey())
        self.http, self.jup, self.rpc, self.dex = http, jup_url.rstrip("/"), rpc, dex
        self.slippage_bps, self.sol_reserve = slippage_bps, sol_reserve
        log.warning("LIVE broker armed for wallet %s", self.owner)

    def sol_price(self) -> float:
        p = self.dex.prices([SOL_MINT]).get(SOL_MINT)
        if not p or p["price"] <= 0:
            raise DataError("no SOL price")
        return p["price"]

    def cash_usd(self) -> float:
        return max(0.0, self.rpc.sol_balance(self.owner) - self.sol_reserve) * self.sol_price()

    def _swap(self, in_mint: str, out_mint: str, amount_raw: int) -> tuple[str, dict]:
        quote = self.http.get(f"{self.jup}/quote", params={
            "inputMint": in_mint, "outputMint": out_mint, "amount": str(amount_raw),
            "slippageBps": self.slippage_bps, "restrictIntermediateTokens": "true"})
        if "outAmount" not in quote:
            raise DataError(f"jupiter quote failed: {quote}")
        swap = self.http.post(f"{self.jup}/swap", json={
            "quoteResponse": quote, "userPublicKey": self.owner, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"})
        from solders.transaction import VersionedTransaction

        raw = VersionedTransaction.from_bytes(base64.b64decode(swap["swapTransaction"]))
        signed = VersionedTransaction(raw.message, [self.kp])
        sig = self.rpc.send_raw(bytes(signed))
        self._confirm(sig)
        return sig, quote

    def _confirm(self, sig: str, timeout_s: float = 60) -> None:
        end = time.time() + timeout_s
        while time.time() < end:
            st = self.rpc.signature_status(sig)
            if st:
                if st.get("err"):
                    raise DataError(f"tx {sig} failed: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            time.sleep(2)
        raise DataError(f"tx {sig} not confirmed in {timeout_s}s")

    def buy(self, mint: str, size_usd: float, ref_price: float) -> Fill:
        lamports = int(size_usd / self.sol_price() * 1e9)
        before = self.rpc.token_balance_raw(self.owner, mint)
        sig, quote = self._swap(SOL_MINT, mint, lamports)
        time.sleep(2)
        got_raw = self.rpc.token_balance_raw(self.owner, mint) - before
        decimals = self.rpc.mint_info(mint)["decimals"]
        qty = (got_raw or int(quote["outAmount"])) / 10 ** decimals
        return Fill(price=size_usd / qty, qty=qty, usd=size_usd, tx=sig)

    def sell(self, pos: Position, ref_price: float) -> Fill:
        bal = self.rpc.token_balance_raw(self.owner, pos.mint)
        if bal <= 0:
            raise DataError(f"no {pos.symbol} balance to sell")
        sol_before = self.rpc.sol_balance(self.owner)
        sig, quote = self._swap(pos.mint, SOL_MINT, bal)
        sol_px = self.sol_price()
        got_sol = self.rpc.sol_balance(self.owner) - sol_before
        if got_sol <= 0:
            got_sol = int(quote["outAmount"]) / 1e9
        usd = got_sol * sol_px
        return Fill(price=usd / pos.qty if pos.qty else 0.0, qty=pos.qty, usd=usd, tx=sig)
