"""CLI (paper only).

  python -m memedesk run         # overnight loop + dashboard on 127.0.0.1:8084
  python -m memedesk once        # one scan cycle, then exit
  python -m memedesk dashboard   # serve the dashboard over the existing data, no trading
  python -m memedesk report      # summary of the last N hours (Grok-written if XAI_API_KEY set)
  python -m memedesk positions   # open + recent positions, both books
  python -m memedesk kills       # most recent kills with reasons
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
    ap.add_argument("cmd", choices=["run", "once", "dashboard", "report", "positions", "kills"])
    ap.add_argument("--env", default=".env")
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("-n", type=int, default=40)
    ap.add_argument("-v", "--verbose", action="store_true", help="also log every handoff")
    args = ap.parse_args(argv)

    cfg = Config.load(args.env)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")

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
        print(desk.report(time.time() - args.hours * 3600))
    return 0


if __name__ == "__main__":
    sys.exit(main())
