"""Measurement layer: rule versions, candidate forward returns, shadow exits, price paths.

Everything here only *records*; nothing changes a trading decision. The point is to judge
rules on data (thousands of candidates, several exit rules on the same entries) instead of
reacting to single charts.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import asdict

SECRET_OR_LOCAL = {"xai_api_key", "telegram_bot_token", "telegram_chat_id", "helius_api_key", "jup_api_key",
                   "coingecko_api_key", "gemini_api_key", "data_dir", "dashboard_host", "dashboard_port",
                   "dashboard", "solana_rpc_url", "smart_wallets_file", "random_seed"}

HORIZONS = (("ret_5m", 300), ("ret_15m", 900), ("ret_1h", 3600), ("ret_4h", 14400), ("ret_24h", 86400))

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY, ts REAL, version TEXT, book TEXT, stage TEXT, mint TEXT, symbol TEXT,
  decision TEXT, reason TEXT, price REAL, liq REAL, mcap REAL, age_h REAL, m5 REAL, h1 REAL,
  ret_5m REAL, ret_15m REAL, ret_1h REAL, ret_4h REAL, ret_24h REAL);
CREATE INDEX IF NOT EXISTS ix_cand_ts ON candidates(ts);
CREATE TABLE IF NOT EXISTS pricepath (pos_id TEXT, ts REAL, price REAL, liq REAL);
CREATE INDEX IF NOT EXISTS ix_path ON pricepath(pos_id, ts);
CREATE TABLE IF NOT EXISTS shadow (
  pos_id TEXT, book TEXT, mint TEXT, symbol TEXT, version TEXT, variant TEXT, entry REAL, opened_ts REAL,
  closed_ts REAL, exit_price REAL, pnl_pct REAL, reason TEXT, PRIMARY KEY (pos_id, variant));
"""


def rule_version(cfg) -> str:
    """Short hash of every trading-relevant setting. Changes whenever a rule changes."""
    d = {k: v for k, v in asdict(cfg).items() if k not in SECRET_OR_LOCAL}
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:8]


# ---- shadow exit variants: alternative exit rules run on the same entry and price path ----------
VARIANTS = {
    #  name            stop%  take-all%  trail_after%  trail%  max_min
    "stop12_hold4h":   (12,   None,      None,         None,   240),
    "stop12_tp25_all": (12,   25,        None,         None,   240),
    "stop12_trail20":  (12,   None,      10,           20,     240),
    "stop12_time30":   (12,   None,      None,         None,   30),
    "stop20_tp50_all": (20,   50,        None,         None,   240),
}
EXIT_COST = 0.01   # same flat exit-cost estimate for every variant, so they compare fairly


class Research:
    def __init__(self, logger, cfg, version: str):
        self.logger, self.cfg, self.version = logger, cfg, version
        with logger.lock:
            logger.db.executescript(SCHEMA)
            logger.db.commit()
        self._seen: dict[tuple, float] = {}
        self._lock = threading.Lock()
        self.rng = random.Random(7)
        self.shadow_open: dict[tuple[str, str], dict] = {}   # (pos_id, variant) -> state
        self._load_open_shadows()

    # ---- candidates -------------------------------------------------------------------------
    def log_candidate(self, book: str, stage: str, c, decision: str, reason: str = "",
                      sample: float = 1.0) -> None:
        """One row per (book, coin, decision) per hour. `sample` < 1 keeps a random share of high-volume rejects."""
        if sample < 1.0 and self.rng.random() > sample:
            return
        key, now = (book, c.mint, decision, reason), time.time()
        with self._lock:
            if now - self._seen.get(key, 0) < 3600:
                return
            self._seen[key] = now
            if len(self._seen) > 50_000:
                self._seen = {k: t for k, t in self._seen.items() if now - t < 3600}
        with self.logger.lock:
            self.logger.db.execute(
                "INSERT INTO candidates(ts,version,book,stage,mint,symbol,decision,reason,price,liq,mcap,age_h,m5,h1) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, self.version, book, stage, c.mint, c.symbol, decision, reason[:60], c.price_usd,
                 c.liquidity_usd, c.mcap, round(c.age_h, 3), c.price_change.get("m5"), c.price_change.get("h1")))
            self.logger.db.commit()

    def follow_up(self, prices, max_mints: int = 300) -> int:
        """Fill forward returns whose horizon has passed. `prices(mints) -> {mint: {'price': ...}}`."""
        now = time.time()
        todo: dict[str, list[tuple[int, str, float]]] = {}
        with self.logger.lock:
            for col, secs in HORIZONS:
                rows = self.logger.db.execute(
                    f"SELECT id, mint, price FROM candidates WHERE {col} IS NULL AND ts <= ? AND ts > ? "
                    "AND price > 0 LIMIT 2000", (now - secs, now - secs - 6 * 3600)).fetchall()
                for rid, mint, p0 in rows:
                    todo.setdefault(mint, []).append((rid, col, p0))
        mints = list(todo)[:max_mints]
        if not mints:
            return 0
        quotes = prices(mints)
        n = 0
        with self.logger.lock:
            for m in mints:
                q = quotes.get(m)
                px = (q or {}).get("price") or 0.0
                for rid, col, p0 in todo[m]:
                    ret = round(100 * (px / p0 - 1), 2) if px > 0 else -100.0   # gone = total loss
                    self.logger.db.execute(f"UPDATE candidates SET {col}=? WHERE id=?", (ret, rid))
                    n += 1
            self.logger.db.commit()
        return n

    # ---- price path ----------------------------------------------------------------------------
    def record_price(self, pos_id: str, price: float, liq) -> None:
        with self.logger.lock:
            self.logger.db.execute("INSERT INTO pricepath(pos_id,ts,price,liq) VALUES(?,?,?,?)",
                                   (pos_id, time.time(), price, liq))
            self.logger.db.commit()

    # ---- shadow exits --------------------------------------------------------------------------
    def _load_open_shadows(self) -> None:
        with self.logger.lock:
            rows = self.logger.db.execute(
                "SELECT pos_id, book, mint, symbol, version, variant, entry, opened_ts FROM shadow "
                "WHERE closed_ts IS NULL").fetchall()
        for pos_id, book, mint, sym, ver, var, entry, opened in rows:
            self.shadow_open[(pos_id, var)] = {"mint": mint, "entry": entry, "opened": opened, "peak": entry,
                                               "book": book, "symbol": sym}

    def start_shadows(self, pos) -> None:
        with self.logger.lock:
            for var in VARIANTS:
                self.logger.db.execute(
                    "INSERT OR IGNORE INTO shadow(pos_id,book,mint,symbol,version,variant,entry,opened_ts) "
                    "VALUES(?,?,?,?,?,?,?,?)", (pos.id, pos.book, pos.mint, pos.symbol, self.version, var,
                                                pos.entry_price, pos.opened_ts))
                self.shadow_open[(pos.id, var)] = {"mint": pos.mint, "entry": pos.entry_price,
                                                   "opened": pos.opened_ts, "peak": pos.entry_price,
                                                   "book": pos.book, "symbol": pos.symbol}
            self.logger.db.commit()

    def shadow_mints(self) -> set[str]:
        return {s["mint"] for s in self.shadow_open.values()}

    def update_shadows(self, quotes: dict, now: float | None = None) -> None:
        now = now or time.time()
        done = []
        for (pos_id, var), s in self.shadow_open.items():
            q = quotes.get(s["mint"])
            price = (q or {}).get("price") or 0.0
            if price <= 0:
                continue
            stop, tp, trail_after, trail, max_min = VARIANTS[var]
            s["peak"] = max(s["peak"], price)
            gain = (price / s["entry"] - 1) * 100
            reason = None
            if gain <= -stop:
                reason = "stop"
            elif tp is not None and gain >= tp:
                reason = "take_all"
            elif trail_after is not None and (s["peak"] / s["entry"] - 1) * 100 >= trail_after \
                    and price <= s["peak"] * (1 - trail / 100):
                reason = "trail"
            elif now - s["opened"] >= max_min * 60:
                reason = "time"
            if reason:
                pnl = round(100 * (price * (1 - EXIT_COST) / s["entry"] - 1), 2)
                done.append((pos_id, var, price, pnl, reason))
        if not done:
            return
        with self.logger.lock:
            for pos_id, var, price, pnl, reason in done:
                self.logger.db.execute("UPDATE shadow SET closed_ts=?, exit_price=?, pnl_pct=?, reason=? "
                                       "WHERE pos_id=? AND variant=?", (now, price, pnl, reason, pos_id, var))
                self.shadow_open.pop((pos_id, var), None)
            self.logger.db.commit()
