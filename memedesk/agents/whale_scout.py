"""Whale Scout: learns which wallets get into winners early, then watches them.

Every 30 min (discovery):
  1. winners   find Solana tokens that ran 5x+ inside the last 7 days
               (GeckoTerminal 4h candles: peak high / lowest low before the peak)
  2. early     read each winner's first ~200 swaps from chain: the pump.fun bonding
               curve for pump mints, else the pool. Helius if keyed, else public RPC.
  3. flags     RugCheck: creator = dev, insider-flagged holders = insider
  4. wallets   score each early buyer: distinct winners bought early + realized PnL
               from every swap of theirs we've seen. Skip bots/MEV (>50 trades/h or
               same-slot buy+sell), devs and insiders. Keep the top 50, drop wallets
               inactive 7 days, log every add/remove with its reason, and write
               smart_wallets.txt.

Between runs (whale mode): poll tracked wallets; when 2+ of them buy the same token
within 10 min, hand that token straight to Filter. It must still pass Filter,
Analyst 70+ and Risk.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from pathlib import Path

from ..config import Config
from ..http import DataError
from ..models import Candidate
from ..sources.solana_util import looks_like_pump_mint, pump_bonding_curve

log = logging.getLogger("memedesk.whale")


def max_multiple(rows: list[list[float]], since_ts: float) -> float:
    """Largest peak-high / prior-lowest-low run-up inside the window."""
    best, low = 0.0, None
    for ts, _o, h, l, _c, _v in rows:
        if ts < since_ts:
            continue
        if l > 0:
            low = l if low is None else min(low, l)
        if low:
            best = max(best, h / low)
    return best


def realized_pnl_sol(swaps) -> float | None:
    """Realized SOL PnL over tokens where we saw both the buys and some sells."""
    per: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])  # buy_sol, buy_qty, sell_sol, sell_qty
    for s in swaps:
        a = per[s.mint]
        if s.d_token > 0:
            a[0] += -s.d_sol
            a[1] += s.d_token
        else:
            a[2] += s.d_sol
            a[3] += -s.d_token
    pnl, seen = 0.0, False
    for buy_sol, buy_qty, sell_sol, sell_qty in per.values():
        if buy_qty > 0 and sell_qty > 0:
            seen = True
            pnl += sell_sol - buy_sol * min(1.0, sell_qty / buy_qty)
    return pnl if seen else None


def bot_reason(swaps, max_per_h: float) -> str | None:
    ts = sorted(s.ts for s in swaps if s.ts)
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > 3600:
            j += 1
        if i - j + 1 > max_per_h:
            return f"bot: {i - j + 1} trades in 1h"
    slots: dict[tuple[int, str], set[str]] = defaultdict(set)
    for s in swaps:
        slots[(s.slot, s.mint)].add(s.side)
    if any(len(v) == 2 for v in slots.values()):
        return "mev: buy and sell of the same token in one slot"
    return None


def wallet_score(n_winners: int, pnl_sol: float | None) -> float:
    w = min(1.0, n_winners / 5)
    if pnl_sol is None:
        return round(80 * w, 1)          # no realized evidence yet: capped at 80
    p = max(0.0, min(1.0, pnl_sol / 20))
    score = 100 * (0.6 * w + 0.4 * p)
    return round(score * (0.5 if pnl_sol < 0 else 1.0), 1)


class WhaleScout:
    name = "whale_scout"

    def __init__(self, cfg: Config, gecko, dex, rug, chain, logger):
        self.cfg, self.gecko, self.dex, self.rug, self.chain, self.logger = cfg, gecko, dex, rug, chain, logger
        self.buys: dict[str, dict[str, tuple[float, int]]] = defaultdict(dict)  # mint -> wallet -> (ts, slot)
        self.signaled: dict[str, float] = {}
        self.last_run = 0.0
        self.last_watch = 0.0

    def _ev(self, kind, mint="", symbol="", reason="", detail="", data=None, level=logging.INFO):
        self.logger.event(self.name, kind, mint, symbol, reason, detail, data, level=level)

    # ---- 1. winners ---------------------------------------------------------
    def find_winners(self) -> list[dict]:
        known = {w["mint"] for w in self.logger.winners()}
        pools = [p for p in self.gecko.discover_pools() if p["mint"] not in known]
        since = time.time() - self.cfg.whale_lookback_d * 86400
        found = []
        for p in pools[: self.cfg.whale_pools_per_run]:
            try:
                mult = max_multiple(self.gecko.ohlcv(p["pool"]), since)
            except DataError as e:
                log.debug("ohlcv %s: %s", p["symbol"], e)
                continue
            if mult >= self.cfg.whale_min_multiple:
                self.logger.save_winner(p["mint"], p["symbol"], p["pool"], round(mult, 1))
                self._ev("winner", p["mint"], p["symbol"], f"{mult:.1f}x",
                         f"{mult:.1f}x within {self.cfg.whale_lookback_d:g}d")
                found.append(p)
        return found

    # ---- 2+3. early buyers + flags -----------------------------------------
    def scan_winner(self, w: dict) -> list[str]:
        mint, sym = w["mint"], w["symbol"]
        n = int(self.cfg.whale_auto("whale_early_swaps"))
        pages = int(self.cfg.whale_auto("whale_max_sig_pages"))
        targets = ([pump_bonding_curve(mint)] if looks_like_pump_mint(mint) else []) + [w["pool"]]
        early: list[str] = []
        status = "no_swaps"
        for addr in targets:
            sigs = self.chain.oldest_signatures(addr, n, pages)
            if sigs is None:
                status = "too_deep"
                continue
            swaps = self.chain.swaps_for([s["signature"] for s in sigs])
            buys = sorted((s for s in swaps if s.mint == mint and s.d_token > 0), key=lambda s: (s.slot, s.ts))
            for s in buys:
                if s.wallet not in early:
                    early.append(s.wallet)
            if early:
                status = "scanned"
                break
        try:
            rep = self.rug.report(mint)
            if rep.get("creator"):
                self.logger.flag_wallet(rep["creator"], mint, "dev")
            for h in rep.get("topHolders") or []:
                if h.get("insider") and h.get("owner"):
                    self.logger.flag_wallet(h["owner"], mint, "insider")
        except DataError as e:
            log.debug("rugcheck %s: %s", sym, e)
        self.logger.save_winner(mint, sym, w["pool"], w["multiple"], status, early)
        self._ev("scan", mint, sym, status, f"{len(early)} early buyers from first {n} swaps "
                 f"via {self.chain.backend}" if early else f"{status} via {self.chain.backend}")
        return early

    # ---- 4. wallet book -----------------------------------------------------
    def rebuild_wallets(self) -> dict:
        cfg, now = self.cfg, time.time()
        early_in: dict[str, list[str]] = defaultdict(list)
        sym = {}
        for w in self.logger.winners("scanned"):
            sym[w["mint"]] = w["symbol"]
            for addr in w["early"]:
                early_in[addr].append(w["mint"])
        flags = self.logger.wallet_flags()
        prev = {w["address"]: w for w in self.logger.tracked_wallets()}
        ranked, excluded = [], {}
        for addr, mints in early_in.items():
            if len(mints) < cfg.whale_min_winners:
                continue
            f = flags.get(addr, set())
            if "dev" in f:
                excluded[addr] = "dev wallet of a winner (RugCheck creator)"
                continue
            if "insider" in f:
                excluded[addr] = "insider flagged by RugCheck"
                continue
            swaps = self.logger.wallet_swaps(addr)
            if why := bot_reason(swaps, cfg.whale_bot_trades_per_h):
                excluded[addr] = why
                continue
            last = self.logger.last_activity(addr)
            if not last or now - last > cfg.whale_inactive_days * 86400:
                excluded[addr] = f"inactive {cfg.whale_inactive_days:g}d"
                continue
            pnl = realized_pnl_sol(swaps)
            ranked.append({"address": addr, "winners": [sym.get(m, m[:6]) for m in mints],
                           "pnl_sol": None if pnl is None else round(pnl, 3),
                           "score": wallet_score(len(mints), pnl), "last_seen": last})
        ranked.sort(key=lambda r: (r["score"], len(r["winners"])), reverse=True)
        keep = ranked[: cfg.whale_top_n]
        cutoff = keep[-1]["score"] if len(keep) == cfg.whale_top_n else None
        keep_set = {r["address"] for r in keep}
        added = removed = 0
        for r in keep:
            pnl = "pnl unknown" if r["pnl_sol"] is None else f"realized {r['pnl_sol']:+.2f} SOL"
            reason = f"early in {len(r['winners'])} winners ({', '.join(r['winners'][:4])}), {pnl}"
            self.logger.upsert_wallet(r["address"], "tracked", r["score"], r["winners"], r["pnl_sol"],
                                      r["last_seen"], reason)
            if r["address"] not in prev:
                added += 1
                self._ev("wallet_add", reason="added", detail=f"{r['address'][:6]}… score {r['score']:.0f}: {reason}",
                         data={"wallet": r["address"], **r})
        for addr, old in prev.items():
            if addr in keep_set:
                continue
            if addr in excluded:
                why = excluded[addr]
            elif cutoff is not None and any(r["address"] == addr for r in ranked):
                why = f"fell out of top {cfg.whale_top_n} (cutoff score {cutoff:.0f})"
            else:
                last = self.logger.last_activity(addr)
                why = (f"inactive {cfg.whale_inactive_days:g}d" if not last or
                       now - last > cfg.whale_inactive_days * 86400 else "no longer qualifies")
            self.logger.upsert_wallet(addr, "dropped", old["score"], old["winners"], old["pnl_sol"],
                                      old["last_seen"], why)
            removed += 1
            self._ev("wallet_drop", reason="removed", detail=f"{addr[:6]}…: {why}", data={"wallet": addr, "why": why})
        self.write_file(keep)
        return {"tracked": len(keep), "added": added, "removed": removed, "excluded": len(excluded)}

    def write_file(self, keep: list[dict]) -> None:
        path = Path(self.cfg.smart_wallets_file)
        lines = ["# Written by Whale Scout. Edits here are overwritten; put your own wallets in "
                 "smart_wallets.manual.txt", f"# updated {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        for r in keep:
            pnl = "?" if r["pnl_sol"] is None else f"{r['pnl_sol']:+.2f}SOL"
            lines.append(f"{r['address']}  # score={r['score']:.0f} winners={len(r['winners'])} pnl={pnl} "
                         f"last_seen={time.strftime('%Y-%m-%d', time.localtime(r['last_seen']))}")
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)

    def run_discovery(self) -> dict:
        t0 = time.time()
        found = self.find_winners()
        pending = self.logger.winners("pending")[: int(self.cfg.whale_auto("whale_tokens_per_run"))]
        for w in pending:
            try:
                self.scan_winner(w)
            except DataError as e:
                if "shutting down" in str(e):
                    raise
                self._ev("error", w["mint"], w["symbol"], "scan_failed", str(e)[:200], level=logging.WARNING)
        book = self.rebuild_wallets()
        self.last_run = time.time()
        out = {"new_winners": len(found), "scanned": len(pending), **book, "secs": round(time.time() - t0)}
        self._ev("run", reason="discovery", detail=str(out), data=out)
        return out

    # ---- whale mode ---------------------------------------------------------
    def watch(self) -> list[dict]:
        """Poll tracked wallets; return signals where 2+ bought the same token within the window."""
        cfg, now = self.cfg, time.time()
        window = cfg.whale_signal_window_min * 60
        tracked = self.logger.tracked_wallets()
        added_ts = {w["address"]: w["added_ts"] for w in tracked}
        for w in tracked:
            try:
                swaps = self.chain.wallet_recent_swaps(w["address"], 15)
            except DataError as e:
                if "shutting down" in str(e):
                    raise
                continue
            for s in swaps:
                if s.d_token > 0 and s.d_sol < 0 and now - s.ts <= window:
                    self.buys[s.mint][s.wallet] = (s.ts, s.slot)
        self.last_watch = time.time()
        signals = []
        for mint, by in list(self.buys.items()):
            by = {w: v for w, v in by.items() if now - v[0] <= window}
            self.buys[mint] = by
            if not by:
                del self.buys[mint]
                continue
            if len(by) < cfg.whale_signal_wallets or now - self.signaled.get(mint, 0) < 3600:
                continue
            times = sorted(v[0] for v in by.values())
            if times[-1] - times[0] > window:
                continue
            slots = {v[1] for v in by.values()}
            n = len(by)
            reason = f"{n} whales same block" if len(slots) == 1 else f"{n} whales in {max(1, round((times[-1] - times[0]) / 60))}m"
            fresh = [w for w in by if added_ts.get(w) and now - added_ts[w] < 86400]
            if fresh:
                reason += " · fresh whale wallet"
            self.signaled[mint] = now
            signals.append({"mint": mint, "wallets": list(by), "reason": reason})
        return signals

    def to_candidate(self, sig: dict) -> Candidate | None:
        from .scout import candidate_from_pair
        best = self.dex.best_pair_per_mint(self.dex.pairs_for_mints([sig["mint"]]), [sig["mint"]])
        pair = best.get(sig["mint"])
        if not pair:
            return None
        c = candidate_from_pair(pair, ["whale"])
        c.trail, c.reason = [self.name], sig["reason"]
        return c
