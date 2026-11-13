"""Logger agent: the desk's single source of truth. Every handoff, every kill,
every fill and every stop move goes to SQLite (queryable) and JSONL (greppable)."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..models import Kill, Position

log = logging.getLogger("memedesk")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, agent TEXT, kind TEXT, mint TEXT,
  symbol TEXT, reason TEXT, detail TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS kills (
  id INTEGER PRIMARY KEY, ts REAL, agent TEXT, mint TEXT, symbol TEXT,
  reason TEXT, detail TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_kills_ts ON kills(ts);
CREATE TABLE IF NOT EXISTS cooldowns (mint TEXT PRIMARY KEY, until REAL, agent TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id TEXT PRIMARY KEY, mint TEXT, status TEXT, opened_ts REAL, body TEXT);
CREATE TABLE IF NOT EXISTS account (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS holders (mint TEXT, ts REAL, holders INTEGER);
CREATE INDEX IF NOT EXISTS ix_holders ON holders(mint, ts);
"""


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


class Logger:
    name = "logger"

    def __init__(self, data_dir: str | Path, mode: str = "paper"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.db = sqlite3.connect(self.dir / f"desk_{mode}.db", check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.jsonl = open(self.dir / f"desk_{mode}.jsonl", "a", buffering=1)
        self.notifier = None  # set by the desk (Telegram)

    # ---- generic event stream --------------------------------------------
    def event(self, agent: str, kind: str, mint: str = "", symbol: str = "",
              reason: str = "", detail: str = "", data: dict | None = None,
              level: int = logging.INFO) -> None:
        ts = time.time()
        with self.lock:
            self.db.execute(
                "INSERT INTO events(ts,agent,kind,mint,symbol,reason,detail,data) VALUES(?,?,?,?,?,?,?,?)",
                (ts, agent, kind, mint, symbol, reason, detail, _json(data or {})))
            self.db.commit()
            self.jsonl.write(_json({"ts": ts, "agent": agent, "kind": kind, "mint": mint,
                                    "symbol": symbol, "reason": reason, "detail": detail,
                                    "data": data or {}}) + "\n")
        log.log(level, "[%-8s] %-9s %-10s %s %s", agent, kind, symbol[:10], reason, detail)

    def handoff(self, src: str, dst: str, mint: str, symbol: str, detail: str = "",
                data: dict | None = None) -> None:
        self.event(src, "handoff", mint, symbol, f"{src}->{dst}", detail, data,
                   level=logging.DEBUG)

    # ---- kills -----------------------------------------------------------
    def kill(self, k: Kill, default_cooldown_min: float) -> None:
        cooldown = default_cooldown_min if k.cooldown_min is None else k.cooldown_min
        with self.lock:
            self.db.execute(
                "INSERT INTO kills(ts,agent,mint,symbol,reason,detail,data) VALUES(?,?,?,?,?,?,?)",
                (k.ts, k.agent, k.mint, k.symbol, k.reason, k.detail, _json(k.data)))
            if cooldown > 0:
                self.db.execute(
                    "INSERT OR REPLACE INTO cooldowns(mint,until,agent,reason) VALUES(?,?,?,?)",
                    (k.mint, k.ts + cooldown * 60, k.agent, k.reason))
            self.db.commit()
        self.event(k.agent, "kill", k.mint, k.symbol, k.reason, k.detail, k.data)

    def on_cooldown(self, mint: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT until FROM cooldowns WHERE mint=?", (mint,)).fetchone()
        return bool(row and row[0] > time.time())

    def set_cooldown(self, mint: str, hours: float, agent: str, reason: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cooldowns(mint,until,agent,reason) VALUES(?,?,?,?)",
                            (mint, time.time() + hours * 3600, agent, reason))
            self.db.commit()

    # ---- holders snapshots (for holder growth) ----------------------------
    def holder_snapshot(self, mint: str, holders: int) -> None:
        with self.lock:
            self.db.execute("INSERT INTO holders(mint,ts,holders) VALUES(?,?,?)",
                            (mint, time.time(), holders))
            self.db.commit()

    def holder_history(self, mint: str, since_s: float = 6 * 3600) -> list[tuple[float, int]]:
        with self.lock:
            return self.db.execute(
                "SELECT ts, holders FROM holders WHERE mint=? AND ts>=? ORDER BY ts",
                (mint, time.time() - since_s)).fetchall()

    # ---- positions & account ---------------------------------------------
    def save_position(self, p: Position) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO positions(id,mint,status,opened_ts,body) VALUES(?,?,?,?,?)",
                (p.id, p.mint, p.status, p.opened_ts, _json(asdict(p))))
            self.db.commit()

    def positions(self, status: str | None = None) -> list[Position]:
        q, args = "SELECT body FROM positions", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY opened_ts", args).fetchall()
        return [Position(**json.loads(r[0])) for r in rows]

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM account WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO account(key,value) VALUES(?,?)",
                            (key, _json(value)))
            self.db.commit()

    def notify(self, text: str) -> None:
        if self.notifier:
            self.notifier.send(text)

    # ---- reporting -------------------------------------------------------
    def summary(self, since_ts: float = 0.0) -> dict:
        with self.lock:
            kills = self.db.execute(
                "SELECT agent, reason FROM kills WHERE ts>=?", (since_ts,)).fetchall()
            scouted = self.db.execute(
                "SELECT COUNT(DISTINCT mint) FROM events WHERE kind='scouted' AND ts>=?",
                (since_ts,)).fetchone()[0]
        by_reason = Counter(f"{a}:{r}" for a, r in kills)
        closed = [p for p in self.positions("closed") if (p.exit_ts or 0) >= since_ts]
        opened = [p for p in self.positions() if p.opened_ts >= since_ts]
        pnl = sum(p.pnl_usd or 0 for p in closed)
        wins = sum(1 for p in closed if (p.pnl_usd or 0) > 0)
        return {
            "mode": self.mode,
            "since": since_ts,
            "unique_mints_scouted": scouted,
            "kills_total": len(kills),
            "kills_by_reason": dict(by_reason.most_common()),
            "entries": len(opened),
            "closed": len(closed),
            "wins": wins,
            "realized_pnl_usd": round(pnl, 2),
            "trades": [{"symbol": p.symbol, "score": p.score, "entry": p.entry_price,
                        "exit": p.exit_price, "reason": p.exit_reason,
                        "pnl_usd": round(p.pnl_usd or 0, 2),
                        "pnl_pct": round(100 * (p.pnl_usd or 0) / p.cost_usd, 1) if p.cost_usd else 0}
                       for p in closed],
            "open_positions": [{"symbol": p.symbol, "entry": p.entry_price, "stop": p.stop_price,
                                "trailing": p.trailing, "last": p.last_price}
                               for p in self.positions("open")],
        }

    def close(self) -> None:
        with self.lock:
            self.db.close()
            self.jsonl.close()
