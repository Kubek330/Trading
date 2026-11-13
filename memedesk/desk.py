"""The desk: wires the six agents with hard handoffs and runs them overnight.

  scout -> filter -> analyst -> executor(propose) -> risk(approve + stop) -> executor(fill)
                                                     risk(monitor) -> executor(close)
  logger records every handoff, kill, fill and stop move.
"""
from __future__ import annotations

import logging
import signal
import threading
import time

from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .agents.scout import Scout
from .broker.paper import PaperBroker
from .config import Config
from .http import HttpClient
from .models import Candidate, Kill
from .sources.bundles import BundleChecker
from .sources.dexscreener import DexScreener
from .sources.geckoterminal import GeckoTerminal
from .sources.grok import Grok, Telegram
from .sources.rugcheck import RugCheck
from .sources.solana_rpc import SolanaRPC

log = logging.getLogger("memedesk")


class Desk:
    def __init__(self, cfg: Config, *, http=None, dex=None, gecko=None, rpc=None, rug=None,
                 bundles=None, grok=None, broker=None, logger=None, smart_wallets=None):
        self.cfg = cfg
        http = http or HttpClient(timeout=cfg.http_timeout_s)
        self.logger = logger or Logger(cfg.data_dir, cfg.mode)
        dex = dex or DexScreener(http, cfg.dexscreener_url)
        gecko = gecko or GeckoTerminal(http, cfg.geckoterminal_url)
        rpc = rpc or SolanaRPC(http, cfg.solana_rpc_url)
        rug = rug or RugCheck(http, cfg.rugcheck_url)
        bundles = bundles or BundleChecker(http, cfg.trench_url)
        self.grok = grok or Grok(http, cfg.grok_url, cfg.xai_api_key, cfg.grok_model)
        self.logger.notifier = Telegram(http, cfg.telegram_bot_token, cfg.telegram_chat_id)
        if broker is None:
            if cfg.mode == "live":
                from .broker.jupiter import JupiterBroker
                broker = JupiterBroker(http, cfg.jupiter_url, rpc, dex, cfg.wallet_keypair_path,
                                       cfg.slippage_bps, cfg.sol_reserve)
            else:
                broker = PaperBroker(self.logger, cfg.paper_start_usd, cfg.slippage_bps, cfg.paper_fee_bps)
        self.broker = broker
        self.scout = Scout(cfg, dex, gecko, self.logger)
        self.filter = Filter(cfg, rpc, rug, bundles, self.logger)
        self.analyst = Analyst(cfg, gecko, self.logger, self.grok, smart_wallets)
        self.executor = Executor(cfg, broker, dex, self.logger)
        self.risk = Risk(cfg, self.logger, dex)
        self.trade_lock = threading.RLock()
        self.stop_event = threading.Event()

    # ---- accounting -------------------------------------------------------
    def equity(self) -> tuple[float, float]:
        cash = self.broker.cash_usd()
        held = sum(p.qty * (p.last_price or p.entry_price) for p in self.logger.positions("open"))
        return cash + held, cash

    # ---- pipeline ---------------------------------------------------------
    def _kill(self, k: Kill) -> None:
        self.logger.kill(k, self.cfg.kill_cooldown_min)

    def _skip(self, mint: str) -> bool:
        return self.logger.on_cooldown(mint) or any(
            p.mint == mint for p in self.logger.positions("open"))

    def process(self, c: Candidate) -> str:
        L = self.logger
        L.event("scout", "scouted", c.mint, c.symbol, "in_window",
                f"age {c.age_h:.1f}h mcap ${c.mcap:,.0f} liq ${c.liquidity_usd:,.0f}",
                data={"pair": c.pair_address, "dex": c.dex_id, "sources": c.sources},
                level=logging.DEBUG)
        L.handoff("scout", "filter", c.mint, c.symbol)
        v = self.filter.run(c)
        if isinstance(v, Kill):
            self._kill(v)
            return "filter"
        L.handoff("filter", "analyst", c.mint, c.symbol, data={"facts": v.facts.__dict__})
        sc = self.analyst.run(v)
        if isinstance(sc, Kill):
            self._kill(sc)
            return "analyst"
        L.event("analyst", "pass", c.mint, c.symbol, f"score {sc.score:.0f}", sc.grok_note,
                data={"components": sc.components, "notes": sc.notes})
        with self.trade_lock:
            equity, cash = self.equity()
            L.handoff("analyst", "executor", c.mint, c.symbol)
            t = self.executor.propose(sc, equity, cash)
            if isinstance(t, Kill):
                self._kill(t)
                return "executor"
            L.handoff("executor", "risk", c.mint, c.symbol, f"size ${t.size_usd:.2f}")
            a = self.risk.pre_trade(t, self.logger.positions("open"))
            if isinstance(a, Kill):
                self._kill(a)
                return "risk"
            L.handoff("risk", "executor", c.mint, c.symbol, f"stop -{a.stop_pct}%")
            try:
                pos = self.executor.execute(t, a)
            except Exception as e:
                self._kill(Kill("executor", c.mint, c.symbol, "fill_failed", str(e), cooldown_min=15))
                return "executor"
        msg = (f"ENTRY {pos.symbol} score {pos.score:.0f} ${pos.cost_usd:.2f} @ {pos.entry_price:.8g} "
               f"stop {pos.stop_price:.8g} (-{pos.stop_pct}%)")
        L.event("executor", "entry", pos.mint, pos.symbol, "filled", msg,
                data={"position": pos.to_dict(), "components": sc.components,
                      "grok": sc.grok_note}, level=logging.WARNING)
        L.notify(f"[{pos.mode}] {msg}\nhttps://dexscreener.com/solana/{pos.pair_address}"
                 + (f"\ngrok: {sc.grok_note}" if sc.grok_note else ""))
        return "entered"

    def scan_cycle(self) -> dict:
        t0 = time.time()
        cands, kills = self.scout.run(skip=self._skip)
        for k in kills:
            self._kill(k)
        outcome: dict[str, int] = {"scout_kills": len(kills)}
        for c in cands:
            if self.stop_event.is_set():
                break
            if self._skip(c.mint):
                continue
            try:
                res = self.process(c)
            except Exception as e:  # one bad token must not stop the night
                log.exception("pipeline error on %s", c.symbol)
                self._kill(Kill("desk", c.mint, c.symbol, "pipeline_error", repr(e)[:300]))
                res = "error"
            outcome[res] = outcome.get(res, 0) + 1
        equity, cash = self.equity()
        self.logger.event("desk", "cycle", reason="scan",
                          detail=f"{len(cands)} in window, {outcome} | equity ${equity:,.2f} cash ${cash:,.2f} "
                                 f"| {time.time() - t0:.0f}s",
                          data={"outcome": outcome, "equity": equity})
        return outcome

    # ---- loops ------------------------------------------------------------
    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.risk.monitor(self.executor, self.trade_lock)
            except Exception:
                log.exception("monitor tick failed")
            self.stop_event.wait(self.cfg.monitor_interval_s)

    def report(self, since_ts: float) -> str:
        s = self.logger.summary(since_ts)
        equity, cash = self.equity()
        s["equity_usd"], s["cash_usd"] = round(equity, 2), round(cash, 2)
        text = self.grok.morning_report(s) if self.grok.enabled else ""
        lines = [f"[{s['mode']}] desk report: equity ${equity:,.2f}, {s['entries']} entries, "
                 f"{s['closed']} closed ({s['wins']} wins), realized ${s['realized_pnl_usd']:+.2f}, "
                 f"{s['kills_total']} kills"]
        top = list(s["kills_by_reason"].items())[:6]
        if top:
            lines.append("top kills: " + ", ".join(f"{k} x{v}" for k, v in top))
        if text:
            lines.append(text)
        return "\n".join(lines)

    def run_forever(self) -> None:
        started = time.time()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stop_event.set())
        equity, _ = self.equity()
        banner = (f"memedesk up in {self.cfg.mode.upper()} mode, equity ${equity:,.2f}, "
                  f"size {self.cfg.position_pct}%/trade, stop -{self.cfg.stop_pct}%, "
                  f"trail {self.cfg.trail_pct}% after +{self.cfg.trail_activate_pct}%, min score {self.cfg.min_score:.0f}")
        self.logger.event("desk", "start", detail=banner, level=logging.WARNING)
        self.logger.notify(banner)
        mon = threading.Thread(target=self._monitor_loop, name="risk-monitor", daemon=True)
        mon.start()
        last_report = time.time()
        while not self.stop_event.is_set():
            try:
                self.scan_cycle()
            except Exception:
                log.exception("scan cycle failed")
            if self.cfg.report_every_h > 0 and time.time() - last_report > self.cfg.report_every_h * 3600:
                self.logger.notify(self.report(last_report))
                last_report = time.time()
            self.stop_event.wait(self.cfg.scan_interval_s)
        mon.join(timeout=5)
        final = self.report(started)
        self.logger.event("desk", "stop", detail=final.splitlines()[0], level=logging.WARNING)
        self.logger.notify(final)
        print(final)
