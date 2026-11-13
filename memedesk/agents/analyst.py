"""Analyst: score vetted coins 0-100. Only >= min_score (70) moves on.

Components (each 0-100, weighted by config):
  momentum       5m / 1h / 6h price change; rewards a climb, fades a parabola
  unique_buyers  distinct buyer wallets on the pool in the last 60 min
  buy_sell       1h buy/sell ratio (txn count and USD volume)
  holder_growth  holder count growth per hour from our own snapshots
  liq_mcap       pool liquidity as % of market cap
  smart_wallets  wallets from smart_wallets.txt seen buying or holding
Missing data scores 0 for that component (never guessed upward).
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path

from ..config import Config
from ..http import DataError
from ..models import Kill, Scorecard, Vetted, require_stage

log = logging.getLogger("memedesk.analyst")


def lin(x: float, lo: float, hi: float) -> float:
    """Map x in [lo, hi] onto [0, 100], clamped."""
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    return max(0.0, min(100.0, 100.0 * (x - lo) / (hi - lo)))


def score_momentum(m5: float, h1: float, h6: float) -> float:
    if h1 < 0:
        h1s = 0.4 * lin(h1, -25, 0)
    elif h1 <= 30:
        h1s = 40 + 0.6 * lin(h1, 0, 20)
    else:  # already extended: fade toward 50 at the +80% filter line
        h1s = 100 - 0.5 * lin(h1, 30, 80)
    return 0.5 * h1s + 0.25 * lin(m5, -5, 5) + 0.25 * lin(h6, -20, 50)


def score_unique_buyers(n: int | None) -> float:
    if n is None:
        return 0.0
    return 100.0 * min(1.0, math.log1p(n) / math.log1p(150))


def score_buy_sell(ratio: float | None) -> float:
    return 0.0 if ratio is None else lin(ratio, 0.8, 2.0)


def score_holder_growth(pct_per_h: float | None) -> float:
    return 0.0 if pct_per_h is None else lin(pct_per_h, 0, 15)


def score_liq_mcap(pct: float) -> float:
    return lin(pct, 5, 35)


def score_smart_wallets(k: int | None) -> float:
    if not k:
        return 0.0
    return {1: 60.0, 2: 85.0}.get(k, 100.0)


def load_smart_wallets(path: str) -> set[str]:
    p = Path(path)
    if not p.is_file():
        return set()
    return {ln.split("#")[0].strip() for ln in p.read_text().splitlines()
            if ln.split("#")[0].strip()}


class Analyst:
    name = "analyst"

    def __init__(self, cfg: Config, gecko, logger, grok=None, smart_wallets: set[str] | None = None):
        self.cfg, self.gecko, self.logger, self.grok = cfg, gecko, logger, grok
        self.smart = smart_wallets if smart_wallets is not None else load_smart_wallets(cfg.smart_wallets_file)
        if not self.smart:
            log.warning("no smart wallets loaded (%s); that component's weight is "
                        "redistributed across the other five", cfg.smart_wallets_file)

    def holder_growth(self, v: Vetted) -> tuple[float | None, str]:
        hist = self.logger.holder_history(v.candidate.mint) if self.logger else []
        if len(hist) >= 2:
            (t0, h0), (t1, h1) = hist[0], hist[-1]
            dt_h = (t1 - t0) / 3600
            if dt_h >= 10 / 60 and h0 > 0:
                return 100.0 * (h1 - h0) / h0 / dt_h, f"measured over {dt_h*60:.0f}m"
        return None, "needs >=10m of holder snapshots"

    def run(self, v: Vetted) -> Scorecard | Kill:
        require_stage(v, "filter", self.name)
        c = v.candidate
        notes: list[str] = []

        trades = None
        try:
            trades = self.gecko.trades(c.pair_address, window_min=60)
        except DataError as e:
            notes.append(f"trade tape unavailable: {e}")

        buyers = {t["wallet"] for t in trades or [] if t["kind"] == "buy" and t["wallet"]}
        tx = c.txns.get("h1", {})
        ratios = []
        if tx.get("sells", 0) > 0:
            ratios.append(tx["buys"] / tx["sells"])
        elif tx.get("buys", 0) > 0:
            ratios.append(3.0)
        if trades:
            bu = sum(t["usd"] for t in trades if t["kind"] == "buy")
            su = sum(t["usd"] for t in trades if t["kind"] == "sell")
            if su > 0:
                ratios.append(bu / su)
        ratio = sum(ratios) / len(ratios) if ratios else None

        growth, growth_note = self.holder_growth(v)
        notes.append(f"holder growth {growth_note}")
        smart_hits = None
        if self.smart:
            seen = buyers | set(v.facts.top_holder_owners)
            smart_hits = len(self.smart & seen)

        comps = {
            "momentum": score_momentum(c.price_change.get("m5", 0), c.price_change.get("h1", 0),
                                       c.price_change.get("h6", 0)),
            "unique_buyers": score_unique_buyers(len(buyers) if trades is not None else None),
            "buy_sell": score_buy_sell(ratio),
            "holder_growth": score_holder_growth(growth),
            "liq_mcap": score_liq_mcap(c.liq_mcap_pct),
            "smart_wallets": score_smart_wallets(smart_hits),
        }
        weights = dict(self.cfg.score_weights)
        if not self.smart:
            weights.pop("smart_wallets")
        total_w = sum(weights.values())
        score = sum(comps[k] * w for k, w in weights.items()) / total_w
        comps = {k: round(val, 1) for k, val in comps.items()}
        raw = {"unique_buyers_1h": len(buyers) if trades is not None else None,
               "buy_sell_ratio": round(ratio, 2) if ratio else None,
               "holder_growth_pct_h": round(growth, 2) if growth is not None else None,
               "smart_wallet_hits": smart_hits, "liq_mcap_pct": round(c.liq_mcap_pct, 1)}

        if score < self.cfg.min_score:
            return Kill(self.name, c.mint, c.symbol, "score_low",
                        f"score {score:.0f} < {self.cfg.min_score:.0f}",
                        data={"score": round(score, 1), "components": comps, "raw": raw},
                        cooldown_min=self.cfg.score_kill_cooldown_min)

        grok_note = ""
        if self.grok and self.grok.enabled:
            veto, grok_note = self.grok.second_opinion({
                "symbol": c.symbol, "age_h": round(c.age_h, 2), "mcap": c.mcap,
                "liquidity": c.liquidity_usd, "price_change": c.price_change,
                "txns": c.txns, "volume": c.volume, "score": round(score, 1),
                "components": comps, "raw": raw, "holders": v.facts.holders,
            })
            if veto and self.cfg.grok_veto:
                return Kill(self.name, c.mint, c.symbol, "grok_veto", grok_note,
                            data={"score": round(score, 1), "components": comps},
                            cooldown_min=self.cfg.score_kill_cooldown_min)

        return Scorecard(vetted=v, score=round(score, 1), components=comps,
                         notes=notes + [f"raw={raw}"], grok_note=grok_note,
                         trail=v.trail + [self.name])
