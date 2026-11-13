"""Paper broker: fills at the live DexScreener price with a slippage + fee
haircut each way. Each book (scored, random) has its own cash."""
from __future__ import annotations

from ..models import Fill


class PaperBroker:
    def __init__(self, logger, start_usd: float, slippage_bps: int, fee_bps: int, books: list[str]):
        self.logger, self.slip, self.fee = logger, slippage_bps / 1e4, fee_bps / 1e4
        for b in books:
            if logger.get(f"cash:{b}") is None:
                logger.put(f"cash:{b}", start_usd)
                logger.put(f"start:{b}", start_usd)

    def cash_usd(self, book: str) -> float:
        return float(self.logger.get(f"cash:{book}", 0.0))

    def start_usd(self, book: str) -> float:
        return float(self.logger.get(f"start:{book}", 0.0))

    def buy(self, book: str, size_usd: float, ref_price: float) -> Fill:
        cash = self.cash_usd(book)
        if size_usd > cash + 1e-9:
            raise RuntimeError(f"{book} cash {cash:.2f} < order {size_usd:.2f}")
        px = ref_price * (1 + self.slip)
        qty = size_usd * (1 - self.fee) / px
        self.logger.put(f"cash:{book}", cash - size_usd)
        return Fill(price=size_usd / qty, qty=qty, usd=size_usd)

    def sell(self, book: str, qty: float, ref_price: float) -> Fill:
        proceeds = qty * ref_price * (1 - self.slip) * (1 - self.fee)
        self.logger.put(f"cash:{book}", self.cash_usd(book) + proceeds)
        return Fill(price=proceeds / qty if qty else 0.0, qty=qty, usd=proceeds)
