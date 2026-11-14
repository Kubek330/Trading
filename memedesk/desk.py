"""The desk: wires the agents with hard handoffs and runs them overnight. Paper only.

  whale scout (every 30 min: winners -> early wallets -> top 50)
     └─ whale mode: 2+ tracked wallets buy the same token within 10 min ─┐
  scout ─────────────────────────────────────────────────────────────────┴─► filter
  filter -+-> analyst -> executor -> risk(stop) -> executor(fill)   [scored book]
          +-> random  -> executor -> risk(stop) -> executor(fill)   [random book]
  risk monitor (both books, same exits) -> executor(sell)
  logger records every handoff, kill, fill, wallet change and stop move.
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.random_control import RandomControl
from .agents.risk import Risk
from .agents.scout import Scout
from .agents.sniper import Sniper
from .agents.whale_scout import WhaleScout
from .broker.paper import PaperBroker
from .config import Config
from .http import DataError, HttpClient
from .models import Candidate, Kill, RandomPick, Scorecard, Vetted
from .sources.bundles import BundleChecker
from .sources.chain import ChainReader
from .sources.dexscreener import DexScreener
from .sources.geckoterminal import GeckoTerminal, MonthlyBudget
from .sources.jupiter import JupiterTokens
from .sources.grok import Grok, Telegram
from .sources.rugcheck import RugCheck
from .sources.solana_rpc import SolanaRPC

log = logging.getLogger("memedesk")

AGENTS = ["whale_scout", "scout", "filter", "analyst", "executor", "risk", "logger", "random", "sniper"]


class Desk:
    def __init__(self, cfg: Config, *, http=None, dex=None, gecko=None, rpc=None, rug=None,
                 bundles=None, grok=None, logger=None, smart_wallets=None, chain=None, jup=None):
        self.cfg = cfg
        self.http = http or HttpClient(timeout=cfg.http_timeout_s, rpm={
            "api.mainnet-beta.solana.com": cfg.rpc_rpm, "mainnet.helius-rpc.com": 500, "api.helius.xyz": 100,
            "api.coingecko.com": 30, "api.jup.ag": 50, "lite-api.jup.ag": 50})
        self.logger = logger or Logger(cfg.data_dir)
        self.agents = {a: {"state": "ready", "last": "", "ts": None} for a in AGENTS}
        if not cfg.whale_scout:
            self.agents["whale_scout"]["state"] = "off"
        if not cfg.random_control:
            self.agents["random"]["state"] = "off"
        if not cfg.sniper:
            self.agents["sniper"]["state"] = "off"
        self.logger.listeners.append(self._on_event)
        self.dex = dex = dex or DexScreener(self.http, cfg.dexscreener_url)
        self.cg_budget = MonthlyBudget(self.logger, "coingecko_demo", cfg.coingecko_monthly_cap)
        gecko = gecko or GeckoTerminal(self.http, cfg.geckoterminal_url, cfg.coingecko_api_key, self.cg_budget)
        self.jup = jup if jup is not None else (
            JupiterTokens(self.http, cfg.jup_api_key) if cfg.min_organic_score > 0 else None)
        self.chain = chain or ChainReader(self.http, cfg.solana_rpc_url, self.logger, cfg.helius_api_key)
        rpc = rpc or SolanaRPC(self.http, self.chain.rpc_url)
        rug = rug or RugCheck(self.http, cfg.rugcheck_url)
        bundles = bundles or BundleChecker(self.http, cfg.trench_url)
        self.grok = grok or Grok(self.http, cfg.grok_url, cfg.xai_api_key, cfg.grok_model)
        self.logger.notifier = Telegram(self.http, cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.broker = PaperBroker(self.logger, cfg.paper_start_usd, cfg.slippage_bps,
                                  cfg.paper_fee_bps, cfg.books)
        self.whale = WhaleScout(cfg, gecko, dex, rug, self.chain, self.logger)
        self.scout = Scout(cfg, dex, gecko, self.logger)
        self.filter = Filter(cfg, rpc, rug, bundles, self.logger, jup=self.jup)
        self.analyst = Analyst(cfg, gecko, self.logger, self.grok, smart_wallets)
        self.random = RandomControl(cfg.random_seed) if cfg.random_control else None
        self.sniper = Sniper(cfg, dex, self.logger) if cfg.sniper else None
        # same on-chain checks, thresholds for brand-new coins (no 1h-pump rule: they're minutes old)
        self.sniper_filter = Filter(replace(cfg, max_top10_pct=cfg.sniper_max_top10_pct, max_dev_pct=cfg.sniper_max_dev_pct,
                                            max_bundle_pct=cfg.sniper_max_bundle_pct, max_h1_change_pct=1e9,
                                            min_organic_score=cfg.sniper_min_organic_score),
                                    rpc, rug, bundles, self.logger, jup=self.jup) if cfg.sniper else None
        self.executor = Executor(cfg, self.broker, dex, self.logger)
        self.risk = Risk(cfg, self.logger, dex)
        self.trade_lock = threading.RLock()
        self.stop_event = getattr(self.http, "stop_event", None) or threading.Event()
        self.whale_queue: queue.Queue[Candidate] = queue.Queue()
        self.started = time.time()
        self.heartbeat = 0.0
        self.last_cycle: dict = {}
        self.recent_vetted: list[Vetted] = []
        self.watch: dict[str, dict] = {}
        if self.random:
            self.logger.event("random", "seed", detail=f"rng seed {self.random.seed}")

    # ---- agent status (dashboard) -----------------------------------------
    def _on_event(self, agent, kind, symbol, reason, detail, data) -> None:
        if kind == "handoff":
            return
        lg = self.agents["logger"]
        lg["last"], lg["ts"] = f"logged {kind}" + (f" · {symbol}" if symbol else ""), time.time()
        a = self.agents.get(agent)
        if not a:
            return
        text = " ".join(x for x in (kind, symbol, reason) if x)
        a["last"], a["ts"] = text[:90], time.time()
        if kind == "error":
            a["state"] = "error"
        elif a["state"] == "error":
            a["state"] = "ready"

    @contextmanager
    def working(self, agent: str):
        a = self.agents[agent]
        a["state"] = "working"
        try:
            yield
        except Exception as e:
            a["state"], a["last"], a["ts"] = "error", f"error: {e}"[:90], time.time()
            raise
        else:
            if a["state"] == "working":
                a["state"] = "ready"

    @property
    def mode(self) -> str:
        tracked = len(self.logger.tracked_wallets()) if self.cfg.whale_mode else 0
        return "whale mode" if tracked >= self.cfg.whale_signal_wallets else "pipeline"

    # ---- accounting -------------------------------------------------------
    def equity(self, book: str) -> tuple[float, float]:
        cash = self.broker.cash_usd(book)
        return cash + sum(p.value_usd for p in self.logger.positions("open", book)), cash

    def _kill(self, k: Kill) -> None:
        self.logger.kill(k, self.cfg.kill_cooldown_min)

    def _blocked(self, mint: str, book: str) -> bool:
        return self.logger.on_cooldown(mint, book) or any(
            p.mint == mint for p in self.logger.positions("open", book))

    def _watch(self, c: Candidate) -> None:
        self.watch[c.mint] = {"mint": c.mint, "symbol": c.symbol, "h1": c.price_change.get("h1"),
                              "pair": c.pair_address, "image": c.image, "mcap": c.mcap, "ts": time.time()}
        if len(self.watch) > 40:
            for m in sorted(self.watch, key=lambda m: self.watch[m]["ts"])[:-40]:
                del self.watch[m]

    # ---- pipeline ---------------------------------------------------------
    def vet(self, c: Candidate) -> Vetted | None:
        L = self.logger
        src = c.trail[-1]
        L.event(src, "scouted", c.mint, c.symbol, c.reason or "in_window",
                f"age {c.age_h:.1f}h mcap ${c.mcap:,.0f} liq ${c.liquidity_usd:,.0f}",
                data={"pair": c.pair_address, "dex": c.dex_id, "sources": c.sources}, level=logging.DEBUG)
        L.handoff(src, "filter", c.mint, c.symbol)
        self._watch(c)
        with self.working("filter"):
            v = self.filter.run(c)
        if isinstance(v, Kill):
            self._kill(v)
            return None
        f = v.facts
        L.event("filter", "pass", c.mint, c.symbol, "safe",
                f"top10 {f.top10_pct:.0f}% · dev {f.dev_pct:.1f}% · LP {f.lp_locked_pct:.0f}% locked"
                if None not in (f.top10_pct, f.dev_pct, f.lp_locked_pct) else "", level=logging.DEBUG)
        return v

    def score(self, v: Vetted) -> Scorecard | None:
        c = v.candidate
        self.logger.handoff("filter", "analyst", c.mint, c.symbol, data={"facts": v.facts.__dict__})
        with self.working("analyst"):
            sc = self.analyst.run(v)
        if isinstance(sc, Kill):
            self._kill(sc)
            return None
        self.logger.event("analyst", "pass", c.mint, c.symbol, f"score {sc.score:.0f}", sc.grok_note,
                          data={"components": sc.components, "notes": sc.notes})
        return sc

    def enter(self, pick: Scorecard | RandomPick) -> str:
        L, c, book = self.logger, pick.vetted.candidate, pick.book
        src = {"scored": "analyst"}.get(book, book)
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
        if book == "random":
            why = f"random pick {pick.index + 1}/{pick.pool_size}"
        elif book == "sniper":
            why = f"snipe · {c.age_h * 60:.0f}m old · momentum {pick.momentum:.0f}"
        else:
            why = (f"{c.reason} · score {pos.score:.0f}" if c.reason else f"score {pos.score:.0f}")
        origin = "whale" if c.trail[0] == "whale_scout" else "pipeline"
        msg = (f"ENTRY {pos.symbol} [{book}] {why} ${pos.cost_usd:.2f} @ {pos.entry_price:.8g} "
               f"stop {pos.stop_price:.8g} (-{self.cfg.rules(book).stop_pct:g}%)")
        L.event("executor", "entry", pos.mint, pos.symbol, book, msg,
                data={"position": pos.to_dict(), "components": getattr(pick, "components", None),
                      "grok": getattr(pick, "grok_note", ""), "origin": origin}, level=logging.WARNING)
        L.event("executor", "decision", pos.mint, pos.symbol, "BUY", why,
                data={"side": "BUY", "book": book, "reason": why, "usd": pos.cost_usd, "origin": origin})
        note = getattr(pick, "grok_note", "")
        L.notify(f"[paper] {msg}\nhttps://dexscreener.com/solana/{pos.pair_address}" + (f"\ngrok: {note}" if note else ""))
        return "entered"

    def _sniper_arm(self, out: dict) -> None:
        if not self.sniper:
            return
        with self.working("sniper"):
            cands = self.sniper.candidates(self.scout.young)
        picks = []
        for c in cands:
            if self.stop_event.is_set():
                break
            if self._blocked(c.mint, "sniper"):
                continue
            k = self.sniper.prefilter(c)
            if k:
                self._kill(k)
                out["sniper_skips"] = out.get("sniper_skips", 0) + 1
                continue
            self.logger.handoff("sniper", "filter", c.mint, c.symbol)
            self._watch(c)
            v = self.sniper_filter.run(c)
            if isinstance(v, Kill):
                v.book = "sniper"              # don't block the main desk from this coin later
                v.agent = "filter"
                self._kill(v)
                out["sniper_filter_kills"] = out.get("sniper_filter_kills", 0) + 1
                continue
            picks.append(self.sniper.pick(v))
        for pick in sorted(picks, key=lambda p: -p.momentum):
            res = self.enter(pick)
            out[f"sniper_{res}"] = out.get(f"sniper_{res}", 0) + 1

    def _random_arm(self, pool: list[Vetted], n: int, out: dict | None = None) -> None:
        if not self.random or n <= 0:
            return
        pool = [v for v in pool if not self._blocked(v.candidate.mint, "random")]
        with self.working("random"):
            picks = self.random.pick(pool, n)
        for pick in picks:
            self.logger.event("random", "pick", pick.vetted.candidate.mint, pick.vetted.candidate.symbol,
                              f"{pick.index + 1}/{pick.pool_size}", f"matched {n} analyst pass(es)")
            res = self.enter(pick)
            if out is not None:
                out[f"random_{res}"] = out.get(f"random_{res}", 0) + 1

    def scan_cycle(self) -> dict:
        t0 = time.time()
        with self.working("scout"):
            cands, kills = self.scout.run(skip=lambda m: self.logger.on_cooldown(m))
        for k in kills:
            self._kill(k)
        self.logger.event("scout", "scan", reason=f"{len(cands)} in window", detail=f"{len(kills)} out of window")
        out: dict[str, int] = {"scout_kills": len(kills), "in_window": len(cands)}
        if self.jup is not None and cands:
            try:  # one Jupiter call scores the whole batch
                self.jup.prefetch([c.mint for c in cands])
            except DataError as e:
                log.warning("jupiter organic prefetch failed: %s", e)

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

        self.recent_vetted = vetted
        self._random_arm(vetted, passes, out)
        try:
            self._sniper_arm(out)
        except Exception:
            log.exception("sniper arm failed")
        eq = {b: round(self.equity(b)[0], 2) for b in self.cfg.books}
        self.last_cycle = {"ts": time.time(), "secs": round(time.time() - t0, 1), "outcome": out, "equity": eq}
        self.logger.event("desk", "cycle", reason="scan",
                          detail=f"{out} | equity {eq} | {time.time() - t0:.0f}s | http {self.http.stats}",
                          data=self.last_cycle)
        return out

    def process_whale(self, c: Candidate) -> str:
        """A whale-mode candidate: straight to Filter, then Analyst 70+, then Risk."""
        v = self.vet(c)
        if not v:
            return "filter"
        if self._blocked(c.mint, "scored"):
            return "blocked"
        sc = self.score(v)
        if not sc:
            return "analyst"
        res = self.enter(sc)
        # keep the control matched: one random draw from the same kind of pool
        self._random_arm(self.recent_vetted + [v], 1)
        return res

    # ---- loops ------------------------------------------------------------
    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self.working("risk"):
                    self.risk.monitor(self.executor, self.trade_lock)
            except Exception:
                log.exception("monitor tick failed")
            self.heartbeat = time.time()
            self.stop_event.wait(self.cfg.monitor_interval_s)

    def whale_tick(self) -> None:
        cfg = self.cfg
        if cfg.whale_scout and time.time() - self.whale.last_run >= cfg.whale_interval_min * 60:
            with self.working("whale_scout"):
                self.whale.run_discovery()
        if cfg.whale_mode and len(self.logger.tracked_wallets()) >= cfg.whale_signal_wallets:
            with self.working("whale_scout"):
                signals = self.whale.watch()
            for s in signals:
                c = self.whale.to_candidate(s)
                if c is None:
                    self.logger.event("whale_scout", "signal", s["mint"], "", "no_pair", s["reason"])
                    continue
                self.logger.event("whale_scout", "signal", c.mint, c.symbol, s["reason"],
                                  f"{len(s['wallets'])} tracked wallets", data=s, level=logging.WARNING)
                self.whale_queue.put(c)

    def _whale_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.whale_tick()
            except DataError as e:
                if "shutting down" not in str(e):
                    log.warning("whale tick: %s", e)
            except Exception:
                log.exception("whale tick failed")
            self.stop_event.wait(self.cfg.whale_auto("whale_watch_interval_s"))

    def report(self, since_ts: float) -> str:
        s = self.logger.summary(self.cfg.books, since_ts)
        lines = ["[paper] desk report"]
        for b, st in s["books"].items():
            lines.append(f"  {b:6}: equity ${st['equity_usd']:,.2f} ({st['return_pct']:+.1f}%), "
                         f"{st['closed']} closed, win {st['win_rate']}%, avg {st['avg_pct']}%/trade, "
                         f"{st['open']} open")
        if s["scored_vs_random"]:
            lines.append(f"  scored vs random: {s['scored_vs_random']['verdict']}")
        lines.append(f"  whale scout: {len(self.logger.tracked_wallets())} tracked wallets, "
                     f"{len(self.logger.winners('scanned'))} winners scanned ({self.chain.backend})")
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
                  f"max {c.max_hold_h:g}h | min score {c.min_score:g} | whale scout "
                  f"{'on' if c.whale_scout else 'off'} via {self.chain.backend}")
        self.logger.event("desk", "start", detail=banner, level=logging.WARNING)
        self.logger.notify(banner)
        threads = [threading.Thread(target=self._monitor_loop, name="risk-monitor", daemon=True)]
        if c.whale_scout or c.whale_mode:
            threads.append(threading.Thread(target=self._whale_loop, name="whale", daemon=True))
        for t in threads:
            t.start()
        last_report, next_scan = time.time(), 0.0
        while not self.stop_event.is_set():
            self.heartbeat = time.time()
            if time.time() >= next_scan:
                try:
                    self.scan_cycle()
                except Exception:
                    log.exception("scan cycle failed")
                next_scan = time.time() + c.scan_interval_s
            if c.report_every_h > 0 and time.time() - last_report > c.report_every_h * 3600:
                self.logger.notify(self.report(last_report))
                last_report = time.time()
            try:  # whale signals jump the queue between scans
                cand = self.whale_queue.get(timeout=2)
            except queue.Empty:
                continue
            try:
                self.process_whale(cand)
            except Exception:
                log.exception("whale candidate failed")
        for t in threads:
            t.join(timeout=5)
        final = self.report(self.started)
        self.logger.event("desk", "stop", detail=final.splitlines()[0], level=logging.WARNING)
        self.logger.notify(final)
        print(final)
