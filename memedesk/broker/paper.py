"""Paper broker: fills at the live DexScreener price with a slippage + fee
haircut each way. Each book (scored, random) has its own cash."""
from __future__ import annotations

from ..models import Fill


VENUE_FEE = {  # swap fee per side, % (DexScreener dexId)
    "pumpfun": 1.0, "pump.fun": 1.0, "moonshot": 1.0, "launchlab": 1.0, "raydium-launchlab": 1.0, "boop": 1.0,
    "believe": 1.0, "pumpswap": 0.30, "raydium": 0.25, "orca": 0.30, "meteora": 0.50, "meteoradbc": 1.0,
}


class PaperBroker:
    """flat: fixed slippage + fee each way (older model, kept for comparison/tests).
    realistic: constant-product price impact from the order's size vs. the pool's quote-side reserve
    (avg fill moves by ~size/reserve), the venue's swap fee, a latency/MEV allowance, and a fixed
    network cost (priority fee + tip) per transaction."""

    def __init__(self, logger, start_usd: float, slippage_bps: int, fee_bps: int, books: list[str],
                 book_slippage_bps: dict[str, int] | None = None, model: str = "flat",
                 base_bps: int = 50, network_usd: float = 0.15):
        self.logger, self.slip, self.fee = logger, slippage_bps / 1e4, fee_bps / 1e4
        self.book_slip = {b: v / 1e4 for b, v in (book_slippage_bps or {}).items()}
        self.model, self.base, self.network = model, base_bps / 1e4, network_usd
        for b in books:
            if logger.get(f"cash:{b}") is None:
                logger.put(f"cash:{b}", start_usd)
                logger.put(f"start:{b}", start_usd)

    def cash_usd(self, book: str) -> float:
        return float(self.logger.get(f"cash:{book}", 0.0))

    def start_usd(self, book: str) -> float:
        return float(self.logger.get(f"start:{book}", 0.0))

    def _realistic(self, liq_usd) -> bool:
        return self.model == "realistic" and liq_usd is not None and liq_usd > 0

    @staticmethod
    def venue_fee(venue: str) -> float:
        return VENUE_FEE.get((venue or "").lower(), 0.30) / 100

    def buy(self, book: str, size_usd: float, ref_price: float, liq_usd: float | None = None,
            venue: str = "") -> Fill:
        cash = self.cash_usd(book)
        if size_usd > cash + 1e-9:
            raise RuntimeError(f"{book} cash {cash:.2f} < order {size_usd:.2f}")
        if self._realistic(liq_usd):
            net = max(0.0, size_usd - self.network) * (1 - self.venue_fee(venue)) * (1 - self.base)
            reserve = liq_usd / 2                       # quote-side reserve of the pool
            avg = ref_price * (1 + net / reserve)       # constant-product average fill
            qty = net / avg if avg > 0 else 0.0
            self.logger.put(f"cash:{book}", cash - size_usd)
            return Fill(price=size_usd / qty if qty else ref_price, qty=qty, usd=size_usd)
        px = ref_price * (1 + self.book_slip.get(book, self.slip))
        qty = size_usd * (1 - self.fee) / px
        self.logger.put(f"cash:{book}", cash - size_usd)
        return Fill(price=size_usd / qty, qty=qty, usd=size_usd)

    def sell(self, book: str, qty: float, ref_price: float, liq_usd: float | None = None, venue: str = "") -> Fill:
        if self._realistic(liq_usd):
            value = qty * ref_price
            reserve = liq_usd / 2
            gross = value / (1 + value / reserve)       # constant-product: selling pushes the price down
            proceeds = max(0.0, gross * (1 - self.venue_fee(venue)) * (1 - self.base) - self.network)
            self.logger.put(f"cash:{book}", self.cash_usd(book) + proceeds)
            return Fill(price=proceeds / qty if qty else 0.0, qty=qty, usd=proceeds)
        proceeds = qty * ref_price * (1 - self.book_slip.get(book, self.slip)) * (1 - self.fee)
        self.logger.put(f"cash:{book}", self.cash_usd(book) + proceeds)
        return Fill(price=proceeds / qty if qty else 0.0, qty=qty, usd=proceeds)


def reset_cash(logger, books: list[str], start_usd: float) -> dict[str, tuple[float, float]]:
    """Put each book back to `start_usd` of equity. Trade history is kept (for `evaluate`); open positions
    stay open and count toward the new equity. Returns {book: (equity before, cash after)}."""
    out = {}
    for b in books:
        opened = logger.positions("open", b)
        held = sum(p.value_usd for p in opened)
        before = float(logger.get(f"cash:{b}", start_usd) or 0.0) + held
        cash = start_usd - held
        logger.put(f"cash:{b}", cash)
        logger.put(f"start:{b}", start_usd)
        with logger.lock:                      # the daily loss limit restarts from the new equity
            logger.db.execute("DELETE FROM account WHERE key LIKE ?", (f"day_start:{b}:%",))
            logger.db.commit()
        logger.event("desk", "reset", reason=b, detail=f"{b}: equity ${before:,.2f} -> ${start_usd:,.2f} "
                     f"({len(opened)} open positions kept)", data={"book": b, "before": round(before, 2)})
        out[b] = (before, cash)
    return out
