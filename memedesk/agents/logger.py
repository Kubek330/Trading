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
CREATE TABLE IF NOT EXISTS watchlist (mint TEXT PRIMARY KEY, symbol TEXT, first_seen REAL, next_check REAL);
CREATE INDEX IF NOT EXISTS ix_watch_due ON watchlist(next_check);
CREATE TABLE IF NOT EXISTS parsed_sigs (sig TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS swaps (
  sig TEXT, slot INTEGER, ts REAL, wallet TEXT, mint TEXT, d_token REAL, d_sol REAL,
  PRIMARY KEY (sig, wallet, mint));
CREATE INDEX IF NOT EXISTS ix_swaps_wallet ON swaps(wallet, ts);
CREATE INDEX IF NOT EXISTS ix_swaps_mint ON swaps(mint, ts);
CREATE TABLE IF NOT EXISTS winners (
  mint TEXT PRIMARY KEY, symbol TEXT, pool TEXT, multiple REAL, found_ts REAL,
  scan_status TEXT, scanned_ts REAL, early_wallets TEXT);
CREATE TABLE IF NOT EXISTS wallet_flags (wallet TEXT, mint TEXT, flag TEXT, PRIMARY KEY (wallet, mint, flag));
CREATE TABLE IF NOT EXISTS wallets (
  address TEXT PRIMARY KEY, status TEXT, score REAL, winners TEXT, pnl_sol REAL,
  last_seen REAL, added_ts REAL, updated_ts REAL, reason TEXT);
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
        self.listeners: list = []   # callables(agent, kind, symbol, reason, detail, data)

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
        for fn in self.listeners:
            try:
                fn(agent, kind, symbol, reason, detail, data or {})
            except Exception:  # a dashboard hook must never break logging
                log.debug("listener failed", exc_info=True)

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

    # ---- scout watchlist ----------------------------------------------------
    def watch_set(self, mint: str, symbol: str, next_check: float) -> None:
        with self.lock:
            self.db.execute("INSERT INTO watchlist(mint,symbol,first_seen,next_check) VALUES(?,?,?,?) "
                            "ON CONFLICT(mint) DO UPDATE SET next_check=excluded.next_check, symbol=excluded.symbol",
                            (mint, symbol, time.time(), next_check))
            self.db.commit()

    def watch_drop(self, mint: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM watchlist WHERE mint=?", (mint,))
            self.db.commit()

    def watch_due(self, limit: int, max_age_s: float = 72 * 3600) -> list[str]:
        now = time.time()
        with self.lock:
            self.db.execute("DELETE FROM watchlist WHERE first_seen < ?", (now - max_age_s,))
            rows = self.db.execute("SELECT mint FROM watchlist WHERE next_check <= ? ORDER BY next_check LIMIT ?",
                                   (now, limit)).fetchall()
            self.db.commit()
        return [r[0] for r in rows]

    def watch_count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]

    # ---- holder snapshots -------------------------------------------------
    def holder_snapshot(self, mint: str, holders: int) -> None:
        with self.lock:
            self.db.execute("INSERT INTO holders(mint,ts,holders) VALUES(?,?,?)", (mint, time.time(), holders))
            self.db.commit()

    def holder_history(self, mint: str, since_s: float = 6 * 3600) -> list[tuple[float, int]]:
        with self.lock:
            return self.db.execute("SELECT ts, holders FROM holders WHERE mint=? AND ts>=? ORDER BY ts",
                                   (mint, time.time() - since_s)).fetchall()

    # ---- chain swap cache (whale scout) -----------------------------------
    def cached_swaps(self, sigs: list[str]):
        from ..sources.chain import Swap
        if not sigs:
            return [], []
        with self.lock:
            done = set()
            rows = []
            for i in range(0, len(sigs), 500):
                chunk = sigs[i:i + 500]
                q = ",".join("?" * len(chunk))
                done |= {r[0] for r in self.db.execute(f"SELECT sig FROM parsed_sigs WHERE sig IN ({q})", chunk)}
                rows += self.db.execute(
                    f"SELECT sig,slot,ts,wallet,mint,d_token,d_sol FROM swaps WHERE sig IN ({q})", chunk).fetchall()
        return [Swap(*r) for r in rows], [x for x in sigs if x not in done]

    def mark_parsed(self, sigs: list[str]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO parsed_sigs(sig) VALUES(?)", [(x,) for x in sigs if x])
            self.db.commit()

    def save_swaps(self, swaps) -> None:
        if not swaps:
            return
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO swaps(sig,slot,ts,wallet,mint,d_token,d_sol) VALUES(?,?,?,?,?,?,?)",
                [(x.sig, x.slot, x.ts, x.wallet, x.mint, x.d_token, x.d_sol) for x in swaps])
            self.db.commit()

    def wallet_swaps(self, wallet: str, since_ts: float = 0.0):
        from ..sources.chain import Swap
        with self.lock:
            rows = self.db.execute("SELECT sig,slot,ts,wallet,mint,d_token,d_sol FROM swaps "
                                   "WHERE wallet=? AND ts>=? ORDER BY ts", (wallet, since_ts)).fetchall()
        return [Swap(*r) for r in rows]

    def last_activity(self, wallet: str) -> float | None:
        with self.lock:
            row = self.db.execute("SELECT MAX(ts) FROM swaps WHERE wallet=?", (wallet,)).fetchone()
        return row[0] if row and row[0] else None

    # ---- wallets & winners ------------------------------------------------
    def tracked_wallets(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT address,score,winners,pnl_sol,last_seen,added_ts,reason FROM wallets "
                                   "WHERE status='tracked' ORDER BY score DESC").fetchall()
        return [{"address": a, "score": sc, "winners": json.loads(w or "[]"), "pnl_sol": p,
                 "last_seen": ls, "added_ts": ad, "reason": r} for a, sc, w, p, ls, ad, r in rows]

    def upsert_wallet(self, address: str, status: str, score: float, winners: list, pnl_sol,
                      last_seen, reason: str) -> None:
        now = time.time()
        with self.lock:
            row = self.db.execute("SELECT added_ts, status FROM wallets WHERE address=?", (address,)).fetchone()
            added = row[0] if row and row[1] == "tracked" and status == "tracked" else (now if status == "tracked" else None)
            self.db.execute(
                "INSERT OR REPLACE INTO wallets(address,status,score,winners,pnl_sol,last_seen,added_ts,updated_ts,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (address, status, score, _json(winners), pnl_sol, last_seen, added, now, reason))
            self.db.commit()

    def flag_wallet(self, wallet: str, mint: str, flag: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO wallet_flags(wallet,mint,flag) VALUES(?,?,?)", (wallet, mint, flag))
            self.db.commit()

    def wallet_flags(self) -> dict[str, set[str]]:
        with self.lock:
            rows = self.db.execute("SELECT wallet, flag FROM wallet_flags").fetchall()
        out: dict[str, set[str]] = {}
        for w, f in rows:
            out.setdefault(w, set()).add(f)
        return out

    def save_winner(self, mint, symbol, pool, multiple, scan_status="pending", early=None) -> None:
        with self.lock:
            old = self.db.execute("SELECT found_ts FROM winners WHERE mint=?", (mint,)).fetchone()
            self.db.execute(
                "INSERT OR REPLACE INTO winners(mint,symbol,pool,multiple,found_ts,scan_status,scanned_ts,early_wallets) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (mint, symbol, pool, multiple, old[0] if old else time.time(), scan_status,
                 time.time() if scan_status != "pending" else None, _json(early or [])))
            self.db.commit()

    def winners(self, status: str | None = None) -> list[dict]:
        q = "SELECT mint,symbol,pool,multiple,found_ts,scan_status,early_wallets FROM winners"
        args: tuple = ()
        if status:
            q, args = q + " WHERE scan_status=?", (status,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY found_ts", args).fetchall()
        return [{"mint": m, "symbol": sy, "pool": p, "multiple": mu, "found_ts": f, "scan_status": st,
                 "early": json.loads(e or "[]")} for m, sy, p, mu, f, st, e in rows]

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
