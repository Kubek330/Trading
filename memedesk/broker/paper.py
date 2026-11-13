"""Paper broker: fills at the live DexScreener price minus a slippage + fee haircut."""
from __future__ import annotations

from ..models import Fill, Position


class PaperBroker:
    mode = "paper"

    def __init__(self, logger, start_usd: float, slippage_bps: int, fee_bps: int):
        self.logger, self.slip, self.fee = logger, slippage_bps / 1e4, fee_bps / 1e4
        if logger.get("paper_cash") is None:
            logger.put("paper_cash", start_usd)
            logger.put("paper_start", start_usd)

    def cash_usd(self) -> float:
        return float(self.logger.get("paper_cash", 0.0))

    def buy(self, mint: str, size_usd: float, ref_price: float) -> Fill:
        cash = self.cash_usd()
        if size_usd > cash + 1e-9:
            raise RuntimeError(f"paper cash {cash:.2f} < order {size_usd:.2f}")
        px = ref_price * (1 + self.slip)
        qty = size_usd * (1 - self.fee) / px
        self.logger.put("paper_cash", cash - size_usd)
        return Fill(price=size_usd / qty, qty=qty, usd=size_usd, tx="paper")

    def sell(self, pos: Position, ref_price: float) -> Fill:
        px = ref_price * (1 - self.slip)
        proceeds = pos.qty * px * (1 - self.fee)
        self.logger.put("paper_cash", self.cash_usd() + proceeds)
        return Fill(price=proceeds / pos.qty if pos.qty else 0.0, qty=pos.qty, usd=proceeds, tx="paper")
