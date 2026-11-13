"""Logger agent: the desk's single source of truth. Every handoff, every kill,
every fill, partial and stop move goes to SQLite (queryable) and JSONL (greppable)."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import threading
import time
from collections import Counter
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
  id INTEGER PRIMARY KEY, ts REAL, agent TEXT, book TEXT, mint TEXT, symbol TEXT,
  reason TEXT, detail TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_kills_ts ON kills(ts);
CREATE TABLE IF NOT EXISTS cooldowns (key TEXT PRIMARY KEY, until REAL, agent TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id TEXT PRIMARY KEY, book TEXT, mint TEXT, status TEXT, opened_ts REAL, body TEXT);
CREATE TABLE IF NOT EXISTS account (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS holders (mint TEXT, ts REAL, holders INTEGER);
CREATE INDEX IF NOT EXISTS ix_holders ON holders(mint, ts);
"""


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


def _ckey(mint: str, book: str | None) -> str:
    return f"{book}:{mint}" if book else mint


class Logger:
    name = "logger"

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "desk_paper.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.jsonl = open(self.dir / "desk_paper.jsonl", "a", buffering=1)
        self.notifier = None

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
        self.event(src, "handoff", mint, symbol, f"{src}->{dst}", detail, data, level=logging.DEBUG)

    # ---- kills & cooldowns -----------------------------------------------
    def kill(self, k: Kill, default_cooldown_min: float) -> None:
        cooldown = default_cooldown_min if k.cooldown_min is None else k.cooldown_min
        with self.lock:
            self.db.execute(
                "INSERT INTO kills(ts,agent,book,mint,symbol,reason,detail,data) VALUES(?,?,?,?,?,?,?,?)",
                (k.ts, k.agent, k.book or "", k.mint, k.symbol, k.reason, k.detail, _json(k.data)))
            if cooldown > 0:
                self.db.execute("INSERT OR REPLACE INTO cooldowns(key,until,agent,reason) VALUES(?,?,?,?)",
                                (_ckey(k.mint, k.book), k.ts + cooldown * 60, k.agent, k.reason))
            self.db.commit()
        self.event(k.agent, "kill", k.mint, k.symbol, k.reason,
                   (f"[{k.book}] " if k.book else "") + k.detail, k.data)

    def on_cooldown(self, mint: str, book: str | None = None) -> bool:
        with self.lock:
            row = self.db.execute("SELECT until FROM cooldowns WHERE key=?", (_ckey(mint, book),)).fetchone()
        return bool(row and row[0] > time.time())

    def set_cooldown(self, mint: str, minutes: float, agent: str, reason: str, book: str | None = None) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO cooldowns(key,until,agent,reason) VALUES(?,?,?,?)",
                            (_ckey(mint, book), time.time() + minutes * 60, agent, reason))
            self.db.commit()

    # ---- holder snapshots -------------------------------------------------
    def holder_snapshot(self, mint: str, holders: int) -> None:
        with self.lock:
            self.db.execute("INSERT INTO holders(mint,ts,holders) VALUES(?,?,?)", (mint, time.time(), holders))
            self.db.commit()

    def holder_history(self, mint: str, since_s: float = 6 * 3600) -> list[tuple[float, int]]:
        with self.lock:
            return self.db.execute("SELECT ts, holders FROM holders WHERE mint=? AND ts>=? ORDER BY ts",
                                   (mint, time.time() - since_s)).fetchall()

    # ---- positions & account ---------------------------------------------
    def save_position(self, p: Position) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO positions(id,book,mint,status,opened_ts,body) VALUES(?,?,?,?,?,?)",
                (p.id, p.book, p.mint, p.status, p.opened_ts, _json(p.to_dict())))
            self.db.commit()

    def positions(self, status: str | None = None, book: str | None = None) -> list[Position]:
        q, args, where = "SELECT body FROM positions", [], []
        if status:
            where.append("status=?"); args.append(status)
        if book:
            where.append("book=?"); args.append(book)
        if where:
            q += " WHERE " + " AND ".join(where)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY opened_ts", args).fetchall()
        return [Position.from_dict(json.loads(r[0])) for r in rows]

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM account WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO account(key,value) VALUES(?,?)", (key, _json(value)))
            self.db.commit()

    def notify(self, text: str) -> None:
        if self.notifier:
            self.notifier.send(text)

    # ---- reporting -------------------------------------------------------
    def book_stats(self, book: str, since_ts: float = 0.0) -> dict:
        closed = [p for p in self.positions("closed", book) if (p.exit_ts or 0) >= since_ts]
        rets = [p.pnl_pct for p in closed if p.pnl_pct is not None]
        opened = self.positions("open", book)
        start = float(self.get(f"start:{book}", 0.0) or 0.0)
        cash = float(self.get(f"cash:{book}", 0.0) or 0.0)
        equity = cash + sum(p.value_usd for p in opened)
        return {
            "book": book,
            "start_usd": start,
            "cash_usd": round(cash, 2),
            "equity_usd": round(equity, 2),
            "return_pct": round(100 * (equity / start - 1), 2) if start else 0.0,
            "entries": len([p for p in self.positions(book=book) if p.opened_ts >= since_ts]),
            "closed": len(closed),
            "open": len(opened),
            "wins": sum(1 for r in rets if r > 0),
            "win_rate": round(100 * sum(1 for r in rets if r > 0) / len(rets), 1) if rets else None,
            "avg_pct": round(statistics.fmean(rets), 2) if rets else None,
            "median_pct": round(statistics.median(rets), 2) if rets else None,
            "realized_usd": round(sum(p.pnl_usd or 0 for p in closed), 2),
            "tp_hits": sum(1 for p in closed if p.tp_taken),
            "exits": dict(Counter(p.exit_reason for p in closed)),
            "_returns": rets,
        }

    @staticmethod
    def compare(scored: dict, rand: dict, min_n: int = 20) -> dict:
        a, b = scored.pop("_returns", []), rand.pop("_returns", [])
        out = {"n_scored": len(a), "n_random": len(b), "verdict": "", "diff_pct": None, "t": None}
        if len(a) < 2 or len(b) < 2:
            out["verdict"] = f"not enough closed trades yet (need {min_n}+ in each book)"
            return out
        ma, mb = statistics.fmean(a), statistics.fmean(b)
        va, vb = statistics.variance(a), statistics.variance(b)
        se = math.sqrt(va / len(a) + vb / len(b)) or 1e-9
        t = (ma - mb) / se  # Welch t
        out.update(diff_pct=round(ma - mb, 2), t=round(t, 2))
        if len(a) < min_n or len(b) < min_n:
            out["verdict"] = f"too early: {len(a)} vs {len(b)} closed trades (need {min_n}+ each)"
        elif t > 2:
            out["verdict"] = "scoring is beating random (t > 2)"
        elif t < -2:
            out["verdict"] = "random is beating scoring (t < -2): the score is hurting"
        else:
            out["verdict"] = "no significant difference yet: scoring hasn't shown edge over luck"
        return out

    def summary(self, books: list[str], since_ts: float = 0.0) -> dict:
        with self.lock:
            kills = self.db.execute("SELECT agent, reason FROM kills WHERE ts>=?", (since_ts,)).fetchall()
            scouted = self.db.execute(
                "SELECT COUNT(DISTINCT mint) FROM events WHERE kind='scouted' AND ts>=?", (since_ts,)).fetchone()[0]
        stats = {b: self.book_stats(b, since_ts) for b in books}
        cmp = self.compare(stats["scored"], stats["random"]) if "random" in stats else None
        for s in stats.values():
            s.pop("_returns", None)
        return {
            "since": since_ts,
            "unique_mints_scouted": scouted,
            "kills_total": len(kills),
            "kills_by_reason": dict(Counter(f"{a}:{r}" for a, r in kills).most_common()),
            "books": stats,
            "scored_vs_random": cmp,
        }

    def close(self) -> None:
        with self.lock:
            self.db.close()
            self.jsonl.close()
