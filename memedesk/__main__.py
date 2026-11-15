"""CLI (paper only).

  python -m memedesk run         # overnight loop + dashboard on 127.0.0.1:8084
  python -m memedesk once        # one scan cycle, then exit
  python -m memedesk dashboard   # serve the dashboard over the existing data, no trading
  python -m memedesk report      # summary of the last N hours (Grok-written if XAI_API_KEY set)
  python -m memedesk positions   # open + recent positions, both books
  python -m memedesk kills       # most recent kills with reasons
  python -m memedesk doctor      # test every data source from this device
  python -m memedesk why         # why nothing gets bought: top kill reasons per book (last --hours, default 1)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import Config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memedesk")
    ap.add_argument("cmd", choices=["run", "once", "dashboard", "report", "positions", "kills", "doctor", "why"])
    ap.add_argument("--env", default=".env")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("-n", type=int, default=40)
    ap.add_argument("-v", "--verbose", action="store_true", help="also log every handoff")
    args = ap.parse_args(argv)

    cfg = Config.load(args.env)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")

    if args.cmd == "doctor":
        from .doctor import run as doctor
        return doctor(cfg)

    if args.cmd == "why":
        from .agents.logger import Logger
        lg = Logger(cfg.data_dir)
        since = time.time() - (args.hours or 1) * 3600
        q = lambda sql, *a: lg.db.execute(sql, (since, *a)).fetchall()  # noqa: E731
        print(f"last {args.hours or 1:g}h · profile {cfg.profile} · min score {cfg.min_score:g}\n")
        scouted = q("SELECT COUNT(DISTINCT mint) FROM events WHERE kind='scouted' AND ts>=?")[0][0]
        print(f"scout: {scouted} coins in the 30m-72h window, watchlist {lg.watch_count()}")
        row = lg.db.execute("SELECT data FROM events WHERE kind='cycle' ORDER BY ts DESC LIMIT 1").fetchone()
        if row:
            lc = json.loads(row[0])
            o, h = lc.get("outcome", {}), lc.get("http", {})
            print(f"last scan: {lc.get('secs')}s · sniper tracking {lc.get('sniper_tracking', '?')} young coins · "
                  f"{o.get('sniper_skips', 0)} sniper skips · http {h.get('requests', '?')} req, "
                  f"{h.get('rate_limited', '?')}x429, {h.get('errors', '?')} errors")
            pp = lc.get("pumpportal")
            if pp:
                print(f"pumpportal live feed: {'connected' if pp.get('connected') else 'DOWN'} · "
                      f"{pp.get('new', 0)} new tokens, {pp.get('migrated', 0)} graduations since start · "
                      f"momentum tracking {lc.get('momentum_tracking', '?')} coins")
            lim = lc.get("limited_by_host") or {}
            if lim:
                print("rate-limited by: " + ", ".join(f"{k} {v}" for k, v in sorted(lim.items(), key=lambda kv: -kv[1])))
        for book, label in (("", "main desk filter (shared)"), ("scored", "analyst / scored book"),
                            ("random", "random book"), ("sniper", "sniper book"),
                            ("momentum", "momentum book")):
            rows = q("SELECT agent, reason, COUNT(*) n, MAX(detail) FROM kills WHERE ts>=? AND book=? "
                     "AND agent!='scout' GROUP BY agent, reason ORDER BY n DESC LIMIT 6", book)
            buys = q("SELECT COUNT(*) FROM events WHERE kind='decision' AND reason='BUY' AND ts>=? "
                     "AND data LIKE ?", f'%"book":"{book}"%')[0][0] if book else None
            head = label + (f" · {buys} bought" if buys is not None else "")
            print(f"\n{head}")
            if not rows:
                print("   (no kills)")
            for agent, reason, n, detail in rows:
                print(f"  {n:5}  {agent:8} {reason:22} e.g. {str(detail)[:60]}")
        return 0

    if args.cmd in ("positions", "kills"):
        from .agents.logger import Logger
        lg = Logger(cfg.data_dir)
        if args.cmd == "positions":
            for p in lg.positions()[-args.n:]:
                pnl = f"{p.pnl_pct:+.1f}%" if p.pnl_pct is not None else f"open {p.unrealized_pct:+.1f}%"
                score = f"{p.score:5.1f}" if p.score is not None else "  rnd"
                print(f"{p.book:6} {p.status:6} {p.symbol[:10]:10} score {score} cost ${p.cost_usd:7.2f} "
                      f"entry {p.entry_price:.6g} stop {p.stop_price:.6g} tp={'Y' if p.tp_taken else 'n'} "
                      f"exit={p.exit_reason} {pnl}")
        else:
            rows = lg.db.execute("SELECT ts,agent,book,symbol,reason,detail FROM kills ORDER BY ts DESC LIMIT ?",
                                 (args.n,)).fetchall()
            for ts, agent, book, sym, reason, detail in rows:
                print(f"{time.strftime('%H:%M:%S', time.localtime(ts))} {agent:8} {book or '-':6} "
                      f"{sym[:10]:10} {reason:22} {detail}")
        return 0

    from .desk import Desk
    desk = Desk(cfg)
    if args.cmd == "run":
        desk.run_forever()
    elif args.cmd == "once":
        print(json.dumps(desk.scan_cycle(), indent=2))
    elif args.cmd == "dashboard":
        from .dashboard import start_dashboard
        if start_dashboard(desk):
            print(f"dashboard on http://{cfg.dashboard_host}:{cfg.dashboard_port} (Ctrl-C to stop)")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    elif args.cmd == "report":
        print(desk.report(time.time() - (args.hours or 12) * 3600))
    return 0


if __name__ == "__main__":
    sys.exit(main())
