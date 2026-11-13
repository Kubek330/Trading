"""The desk: wires the agents with hard handoffs and runs them overnight. Paper only.

  scout -> filter -+-> analyst -> executor -> risk(stop) -> executor(fill)   [scored book]
                   +-> random  -> executor -> risk(stop) -> executor(fill)   [random book]
  risk monitor (both books, same exits) -> executor(sell)
  logger records every handoff, kill, fill, partial and stop move.
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
from .agents.random_control import RandomControl
from .agents.risk import Risk
from .agents.scout import Scout
from .broker.paper import PaperBroker
from .config import Config
from .http import HttpClient
from .models import Candidate, Kill, RandomPick, Scorecard, Vetted
from .sources.bundles import BundleChecker
from .sources.dexscreener import DexScreener
from .sources.geckoterminal import GeckoTerminal
from .sources.grok import Grok, Telegram
from .sources.rugcheck import RugCheck
from .sources.solana_rpc import SolanaRPC

log = logging.getLogger("memedesk")


class Desk:
    def __init__(self, cfg: Config, *, http=None, dex=None, gecko=None, rpc=None, rug=None,
                 bundles=None, grok=None, logger=None, smart_wallets=None):
        self.cfg = cfg
        self.http = http or HttpClient(timeout=cfg.http_timeout_s,
                                       rpm={"api.mainnet-beta.solana.com": cfg.rpc_rpm})
        self.logger = logger or Logger(cfg.data_dir)
        dex = dex or DexScreener(self.http, cfg.dexscreener_url)
        gecko = gecko or GeckoTerminal(self.http, cfg.geckoterminal_url)
        rpc = rpc or SolanaRPC(self.http, cfg.solana_rpc_url)
        rug = rug or RugCheck(self.http, cfg.rugcheck_url)
        bundles = bundles or BundleChecker(self.http, cfg.trench_url)
        self.grok = grok or Grok(self.http, cfg.grok_url, cfg.xai_api_key, cfg.grok_model)
        self.logger.notifier = Telegram(self.http, cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.broker = PaperBroker(self.logger, cfg.paper_start_usd, cfg.slippage_bps,
                                  cfg.paper_fee_bps, cfg.books)
        self.scout = Scout(cfg, dex, gecko, self.logger)
        self.filter = Filter(cfg, rpc, rug, bundles, self.logger)
        self.analyst = Analyst(cfg, gecko, self.logger, self.grok, smart_wallets)
        self.random = RandomControl(cfg.random_seed) if cfg.random_control else None
        self.executor = Executor(cfg, self.broker, dex, self.logger)
        self.risk = Risk(cfg, self.logger, dex)
        self.trade_lock = threading.RLock()
        self.stop_event = getattr(self.http, "stop_event", None) or threading.Event()
        self.started = time.time()
        self.last_cycle: dict = {}
        if self.random:
            self.logger.event("random", "seed", detail=f"rng seed {self.random.seed}")

    # ---- accounting -------------------------------------------------------
    def equity(self, book: str) -> tuple[float, float]:
        cash = self.broker.cash_usd(book)
        return cash + sum(p.value_usd for p in self.logger.positions("open", book)), cash

    def _kill(self, k: Kill) -> None:
        self.logger.kill(k, self.cfg.kill_cooldown_min)

    def _blocked(self, mint: str, book: str) -> bool:
        return self.logger.on_cooldown(mint, book) or any(
            p.mint == mint for p in self.logger.positions("open", book))

    # ---- pipeline ---------------------------------------------------------
    def vet(self, c: Candidate) -> Vetted | None:
        L = self.logger
        L.event("scout", "scouted", c.mint, c.symbol, "in_window",
                f"age {c.age_h:.1f}h mcap ${c.mcap:,.0f} liq ${c.liquidity_usd:,.0f}",
                data={"pair": c.pair_address, "dex": c.dex_id, "sources": c.sources}, level=logging.DEBUG)
        L.handoff("scout", "filter", c.mint, c.symbol)
        v = self.filter.run(c)
        if isinstance(v, Kill):
            self._kill(v)
            return None
        return v

    def score(self, v: Vetted) -> Scorecard | None:
        c = v.candidate
        self.logger.handoff("filter", "analyst", c.mint, c.symbol, data={"facts": v.facts.__dict__})
        sc = self.analyst.run(v)
        if isinstance(sc, Kill):
            self._kill(sc)
            return None
        self.logger.event("analyst", "pass", c.mint, c.symbol, f"score {sc.score:.0f}", sc.grok_note,
                          data={"components": sc.components, "notes": sc.notes})
        return sc

    def enter(self, pick: Scorecard | RandomPick) -> str:
        L, c, book = self.logger, pick.vetted.candidate, pick.book
        src = "analyst" if book == "scored" else "random"
        with self.trade_lock:
            equity, cash = self.equity(book)
            L.handoff(src, "executor", c.mint, c.symbol, book)
            t = self.executor.propose(pick, equity, cash)
            if isinstance(t, Kill):
                self._kill(t)
                return "executor"
            L.handoff("executor", "risk", c.mint, c.symbol, f"{book} size ${t.size_usd:.2f}")
            a = self.risk.pre_trade(t, self.logger.positions("open"))
            if isinstance(a, Kill):
                self._kill(a)
                return "risk"
            L.handoff("risk", "executor", c.mint, c.symbol, f"{book} stop -{a.stop_pct}%")
            try:
                pos = self.executor.execute(t, a)
            except Exception as e:
                self._kill(Kill("executor", c.mint, c.symbol, "fill_failed", str(e), cooldown_min=15, book=book))
                return "executor"
        tag = f"score {pos.score:.0f}" if pos.score is not None else f"random pick {pick.index + 1}/{pick.pool_size}"
        msg = (f"ENTRY {pos.symbol} [{book}] {tag} ${pos.cost_usd:.2f} @ {pos.entry_price:.8g} "
               f"stop {pos.stop_price:.8g} (-{self.cfg.stop_pct:g}%)")
        L.event("executor", "entry", pos.mint, pos.symbol, book, msg,
                data={"position": pos.to_dict(),
                      "components": getattr(pick, "components", None),
                      "grok": getattr(pick, "grok_note", "")}, level=logging.WARNING)
        note = getattr(pick, "grok_note", "")
        L.notify(f"[paper] {msg}\nhttps://dexscreener.com/solana/{pos.pair_address}" + (f"\ngrok: {note}" if note else ""))
        return "entered"

    def scan_cycle(self) -> dict:
        t0 = time.time()
        cands, kills = self.scout.run(skip=lambda m: self.logger.on_cooldown(m))
        for k in kills:
            self._kill(k)
        out: dict[str, int] = {"scout_kills": len(kills), "in_window": len(cands)}

        def bump(k):
            out[k] = out.get(k, 0) + 1

        vetted: list[Vetted] = []
        for c in cands:
            if self.stop_event.is_set():
                break
            try:
                v = self.vet(c)
            except Exception as e:  # one bad token must not stop the night
                log.exception("filter error on %s", c.symbol)
                self._kill(Kill("desk", c.mint, c.symbol, "pipeline_error", repr(e)[:300]))
                continue
            if v:
                vetted.append(v)
            else:
                bump("filter_kills")
        out["vetted"] = len(vetted)

        passes = 0
        for v in vetted:
            if self.stop_event.is_set() or self._blocked(v.candidate.mint, "scored"):
                continue
            try:
                sc = self.score(v)
            except Exception as e:
                log.exception("analyst error on %s", v.candidate.symbol)
                self._kill(Kill("desk", v.candidate.mint, v.candidate.symbol, "pipeline_error",
                                repr(e)[:300], book="scored"))
                continue
            if not sc:
                bump("analyst_kills")
                continue
            passes += 1
            bump(f"scored_{self.enter(sc)}")

        if self.random:
            pool = [v for v in vetted if not self._blocked(v.candidate.mint, "random")]
            for pick in self.random.pick(pool, passes):
                self.logger.event("random", "pick", pick.vetted.candidate.mint, pick.vetted.candidate.symbol,
                                  f"{pick.index + 1}/{pick.pool_size}", f"matched {passes} analyst pass(es)",
                                  level=logging.DEBUG)
                bump(f"random_{self.enter(pick)}")

        eq = {b: round(self.equity(b)[0], 2) for b in self.cfg.books}
        self.last_cycle = {"ts": time.time(), "secs": round(time.time() - t0, 1), "outcome": out, "equity": eq}
        self.logger.event("desk", "cycle", reason="scan",
                          detail=f"{out} | equity {eq} | {time.time() - t0:.0f}s | http {self.http.stats}",
                          data=self.last_cycle)
        return out

    # ---- loops ------------------------------------------------------------
    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.risk.monitor(self.executor, self.trade_lock)
            except Exception:
                log.exception("monitor tick failed")
            self.stop_event.wait(self.cfg.monitor_interval_s)

    def report(self, since_ts: float) -> str:
        s = self.logger.summary(self.cfg.books, since_ts)
        lines = ["[paper] desk report"]
        for b, st in s["books"].items():
            lines.append(f"  {b:6}: equity ${st['equity_usd']:,.2f} ({st['return_pct']:+.1f}%), "
                         f"{st['closed']} closed, win {st['win_rate']}%, avg {st['avg_pct']}%/trade, "
                         f"{st['open']} open")
        if s["scored_vs_random"]:
            lines.append(f"  scored vs random: {s['scored_vs_random']['verdict']}")
        top = list(s["kills_by_reason"].items())[:6]
        if top:
            lines.append("  top kills: " + ", ".join(f"{k} x{v}" for k, v in top))
        if self.grok.enabled:
            lines.append(self.grok.morning_report(s))
        return "\n".join(lines)

    def run_forever(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stop_event.set())
        if self.cfg.dashboard:
            from .dashboard import start_dashboard
            start_dashboard(self)
        c = self.cfg
        banner = (f"memedesk PAPER up | books {c.books} ${c.paper_start_usd:,.0f} each | "
                  f"{c.position_pct:g}%/trade | stop -{c.stop_pct:g}% | +{c.tp_trigger_pct:g}% sell "
                  f"{c.tp_sell_pct:g}% | runner trails {c.runner_trail_pct:g}% (floor entry) | "
                  f"max {c.max_hold_h:g}h | min score {c.min_score:g}")
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
            if c.report_every_h > 0 and time.time() - last_report > c.report_every_h * 3600:
                self.logger.notify(self.report(last_report))
                last_report = time.time()
            self.stop_event.wait(c.scan_interval_s)
        mon.join(timeout=5)
        final = self.report(self.started)
        self.logger.event("desk", "stop", detail=final.splitlines()[0], level=logging.WARNING)
        self.logger.notify(final)
        print(final)
