"""MemeDesk dashboard (stdlib http.server) on http://127.0.0.1:8084.

Read-only: it never places or changes trades. Every number on the page comes
from the desk's own state (SQLite + in-memory agent status); nothing is
invented. All strings that originate on-chain (token names) are escaped.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("memedesk.dashboard")

AGENT_INFO = {
    "whale_scout": ("Whale Scout", "finds 5x winners, tracks their earliest wallets"),
    "scout": ("Scout", "pairs 30m–72h old, $300k–$20M mcap"),
    "filter": ("Filter", "kills unsafe: authorities, LP, holders, dev, bundles"),
    "analyst": ("Analyst", "scores 0–100, only 70+ passes"),
    "risk": ("Risk", "stop before entry, take-profit, trailing runner"),
    "executor": ("Executor", "paper fills, max 8% per trade"),
    "logger": ("Logger", "records every kill, fill and wallet change"),
    "random": ("Random", "luck baseline: skips the analyst"),
}


def _safe_img(url) -> str:
    """Only plain https image URLs reach the page (escaped again client-side)."""
    url = str(url or "")
    return url if url.startswith("https://") and len(url) < 400 and '"' not in url else ""


def _book_view(desk, book: str, now: float, curve: list[dict]) -> dict:
    L = desk.logger
    st = L.book_stats(book)
    st.pop("_returns", None)
    closed = L.positions("closed", book)
    wins = sum(1 for p in closed if (p.pnl_usd or 0) > 0)
    quotes = desk.risk.quotes
    positions = []
    for p in L.positions("open", book):
        q = quotes.get(p.mint, {})
        w = desk.watch.get(p.mint, {})
        positions.append({
            "symbol": p.symbol, "mint": p.mint, "pair": p.pair_address, "score": p.score,
            "mcap": q.get("mcap") or w.get("mcap"), "image": _safe_img(q.get("image") or w.get("image")),
            "entry": p.entry_price, "now": p.last_price,
            "pnl_usd": round(p.realized_usd + p.value_usd - p.cost_usd, 2),
            "pnl_pct": round(p.unrealized_pct, 1), "tp_taken": p.tp_taken,
            "stop": p.stop_price, "age_h": round((now - p.opened_ts) / 3600, 1)})
    # change over the last hour, from the desk's own equity snapshots (None until there is an hour of history)
    hour = [c for c in curve if c.get(book) is not None and c["ts"] <= now - 3600]
    delta_1h = round(st["equity_usd"] - hour[-1][book], 2) if hour else None
    return {**st, "wins": wins, "losses": len(closed) - wins, "positions": positions, "delta_1h": delta_1h,
            "pnl_usd": round(st["equity_usd"] - st["start_usd"], 2),
            "multiple": round(st["equity_usd"] / st["start_usd"], 3) if st["start_usd"] else None}


def _funnel(L, since: float) -> dict:
    with L.lock:
        q = lambda sql: L.db.execute(sql, (since,)).fetchone()[0]  # noqa: E731
        scouted = q("SELECT COUNT(DISTINCT mint) FROM events WHERE kind='scouted' AND ts>=?")
        vetted = q("SELECT COUNT(DISTINCT mint) FROM events WHERE kind='pass' AND agent='filter' AND ts>=?")
        scored = q("SELECT COUNT(DISTINCT mint) FROM events WHERE kind='pass' AND agent='analyst' AND ts>=?")
        bought = q("SELECT COUNT(*) FROM events WHERE kind='decision' AND reason='BUY' AND ts>=? "
                   "AND data LIKE '%\"book\":\"scored\"%'")
        reasons = L.db.execute("SELECT agent, reason, COUNT(*) n FROM kills WHERE ts>=? AND agent!='scout' "
                               "GROUP BY agent, reason ORDER BY n DESC LIMIT 6", (since,)).fetchall()
    return {"scouted": scouted, "vetted": vetted, "scored": scored, "bought": bought,
            "reasons": [{"agent": a, "reason": r, "n": n} for a, r, n in reasons]}


def build_state(desk) -> dict:
    L, cfg, now = desk.logger, desk.cfg, time.time()
    with L.lock:
        dec_rows = L.db.execute("SELECT ts,symbol,mint,data FROM events WHERE kind='decision' "
                                "ORDER BY ts DESC LIMIT 80").fetchall()
        curve_rows = L.db.execute("SELECT ts,data FROM events WHERE kind='cycle' "
                                  "ORDER BY ts DESC LIMIT 720").fetchall()
    curve = []
    for ts, data in reversed(curve_rows):
        try:
            curve.append({"ts": ts, **json.loads(data).get("equity", {})})
        except ValueError:
            pass
    # ticker: tokens the desk is actually watching (scout window + whale signals + open positions)
    watch = {m: dict(w) for m, w in desk.watch.items()}
    for m, q in desk.risk.quotes.items():
        w = watch.setdefault(m, {"mint": m, "symbol": q.get("symbol", ""), "ts": q["ts"]})
        if q.get("h1") is not None:
            w["h1"] = q["h1"]
        for k in ("symbol", "image"):
            if not w.get(k) and q.get(k):
                w[k] = q[k]
    images = {m: _safe_img(w.get("image")) for m, w in watch.items()}
    decisions = []
    for ts, sym, mint, data in dec_rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        decisions.append({"ts": ts, "symbol": sym, "mint": mint, "side": d.get("side"), "book": d.get("book"),
                          "reason": d.get("reason"), "usd": d.get("usd"), "origin": d.get("origin"),
                          "pnl_usd": d.get("pnl_usd"),
                          "image": images.get(mint, "")})
    ticker = [{"symbol": w.get("symbol", ""), "h1": w.get("h1"), "image": _safe_img(w.get("image")),
               "pair": w.get("pair", "")} for w in sorted(watch.values(), key=lambda w: -w["ts"])[:30]]
    tracked = L.tracked_wallets()
    cmp = L.summary(cfg.books)["scored_vs_random"]
    info = dict(AGENT_INFO, analyst=("Analyst", f"scores 0–100, only {cfg.min_score:g}+ passes"))
    agents = {k: {**v, "name": info[k][0], "desc": info[k][1]} for k, v in desk.agents.items()}
    return {
        "now": now, "started": desk.started, "heartbeat": desk.heartbeat, "mode": desk.mode,
        "profile": cfg.profile,
        "books": {b: _book_view(desk, b, now, curve) for b in cfg.books}, "compare": cmp,
        "curve": curve, "decisions": decisions, "ticker": ticker, "agents": agents,
        "funnel": _funnel(L, now - 3600),
        "wallets": [{"address": w["address"], "score": w["score"], "winners": len(w["winners"]),
                     "pnl_sol": w["pnl_sol"], "last_seen": w["last_seen"]} for w in tracked],
        "whale": {"backend": desk.chain.backend, "last_run": desk.whale.last_run,
                  "last_watch": desk.whale.last_watch, "winners_scanned": len(L.winners("scanned")),
                  "winners_pending": len(L.winners("pending"))},
        "last_cycle": desk.last_cycle, "http": desk.http.stats,
        "coingecko": {"enabled": bool(cfg.coingecko_api_key), "used": desk.cg_budget.used,
                      "cap": cfg.coingecko_monthly_cap},
        "rules": {"stop": cfg.stop_pct, "tp_trigger": cfg.tp_trigger_pct, "tp_sell": cfg.tp_sell_pct,
                  "runner_trail": cfg.runner_trail_pct, "max_hold_h": cfg.max_hold_h,
                  "size_pct": cfg.position_pct, "min_score": cfg.min_score, "scan_s": cfg.scan_interval_s,
                  "monitor_s": cfg.monitor_interval_s},
    }


def start_dashboard(desk) -> ThreadingHTTPServer | None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                try:
                    body = json.dumps(build_state(desk), default=str).encode()
                except Exception as e:  # never take the desk down
                    log.exception("dashboard state failed")
                    body = json.dumps({"error": str(e)}).encode()
                ctype = "application/json"
            elif path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer((desk.cfg.dashboard_host, desk.cfg.dashboard_port), Handler)
    except OSError as e:
        log.error("dashboard could not bind %s:%s: %s", desk.cfg.dashboard_host, desk.cfg.dashboard_port, e)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True).start()
    log.warning("dashboard on http://%s:%s", desk.cfg.dashboard_host, srv.server_address[1])
    return srv




PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#05070a">
<title>MemeDesk</title>
<style>
:root{--bg:#05070a;--panel:#0b0f14;--panel2:#11161d;--line:#1a2129;--ink:#e9eef3;--mute:#7d8a97;--dim:#3c4752;
--up:#35d07f;--down:#ff5d5d;--buy:#5aa9ff;
--whale_scout:#ff9f43;--scout:#3ddc84;--filter:#ffd166;--analyst:#e9eef3;--risk:#ff5d73;--executor:#4cc9f0;--logger:#b39dfa;--random:#f78fb3;
--mono:ui-monospace,"JetBrains Mono","SFMono-Regular",Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:13px/1.45 var(--mono);-webkit-font-smoothing:antialiased}
main{max-width:520px;margin:0 auto;padding:10px 16px 56px}
a{color:inherit;text-decoration:none}
.mute{color:var(--mute)} .up{color:var(--up)} .down{color:var(--down)}
header{display:flex;align-items:center;gap:10px;padding:6px 0 2px}
.logo{width:40px;height:40px;border-radius:12px;background:#e9eef3;display:grid;place-items:center;flex:none}
header h1{font:800 24px var(--sans);margin:0;letter-spacing:-.3px}
.pill{font:12px var(--sans);padding:3px 10px;border-radius:99px;background:var(--panel2);color:var(--mute);white-space:nowrap}
.live{margin-left:auto;display:flex;align-items:center;gap:7px;font:600 13px var(--sans);padding:6px 12px;border-radius:99px;
 background:var(--panel2);border:1px solid var(--line);white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--up);animation:pulse 2s infinite} .dot.bad{background:var(--down)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(53,208,127,.6)}70%{box-shadow:0 0 0 7px rgba(53,208,127,0)}100%{box-shadow:0 0 0 0 rgba(53,208,127,0)}}
.subline{color:var(--mute);font-size:12px;margin:6px 0 10px;letter-spacing:.3px}
.ticker{overflow:hidden;margin:0 -16px 6px;mask-image:linear-gradient(90deg,transparent,#000 5%,#000 95%,transparent)}
.ticker .track{display:inline-flex;gap:8px;padding:2px 16px;white-space:nowrap;animation:tick var(--dur,40s) linear infinite}
.tk{display:inline-flex;align-items:center;gap:7px;padding:5px 12px 5px 6px;border-radius:99px;background:var(--panel2);border:1px solid var(--line);font-size:12px}
.tk b{font-weight:700}
@keyframes tick{to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.ticker{overflow-x:auto}.ticker .track{animation:none}.dot.on{animation:none}}
.av{width:22px;height:22px;border-radius:50%;flex:none;object-fit:cover;background:var(--panel);display:inline-grid;place-items:center;
 font:700 10px var(--sans);color:var(--bg)}
.av.lg{width:34px;height:34px;font-size:13px}
.av{position:relative;overflow:hidden}.av img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.hero{padding:14px 0 0;position:relative}
.bal{font:800 58px/1 var(--sans);letter-spacing:-2px;font-variant-numeric:tabular-nums;margin:6px 0 12px}
.bal .c{color:var(--dim)}
.dpill{position:absolute;right:0;top:30px;font:700 13px var(--sans);padding:6px 10px;border-radius:10px}
.dpill.up{background:rgba(53,208,127,.12)} .dpill.down{background:rgba(255,93,93,.12)}
.stats{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.box{padding:6px 12px;border-radius:10px;background:var(--panel2);border:1px solid var(--line);color:var(--mute);font-size:13px}
.mult{padding:6px 12px;border-radius:10px;background:#e9eef3;color:#05070a;font:800 14px var(--sans)}
.mult.down{background:#ffd9d9}
.pnlrow{display:flex;justify-content:space-between;margin-top:12px;font-size:14px}
.chart{height:150px;margin:16px 0 4px;position:relative}
.chart svg{width:100%;height:100%;display:block;overflow:visible}
.axis{display:flex;justify-content:space-between;color:var(--mute);font-size:11px;margin-bottom:8px}
.chips{display:flex;gap:6px;overflow-x:auto;padding:10px 0 4px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;display:flex;align-items:center;gap:6px;font:600 11px var(--mono);padding:5px 10px 5px 6px;border-radius:10px;
 background:var(--panel2);border:1px solid var(--line);letter-spacing:.4px;cursor:pointer;text-transform:uppercase}
.chip.working{border-color:color-mix(in srgb,var(--c) 60%,transparent)} .chip.error{border-color:var(--down)} .chip.off{opacity:.4}
.tabs{display:flex;gap:4px;margin:12px 0;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:4px}
.tabs button{flex:1;background:none;border:0;color:var(--mute);font:600 13px var(--sans);padding:9px 0;border-radius:9px;cursor:pointer}
.tabs button.on{background:var(--panel2);color:var(--ink)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px;margin-bottom:12px}
.card h2{margin:0 0 6px;font:700 17px var(--sans);display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.card h2 small{font:12px var(--mono);color:var(--mute)}
.prow{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:center;padding:9px 0}
.prow .n{font:700 15px var(--sans);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prow .n span{font:12px var(--mono);color:var(--mute);margin-left:6px}
.prow .s{font-size:11px;color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prow .v{font:700 15px var(--mono);text-align:right}
.prow .v small{display:block;font:11px var(--mono);color:var(--mute)}
.drow{display:grid;grid-template-columns:40px 36px 22px minmax(0,1fr);gap:8px;align-items:center;padding:6px 0;font-size:12px}
.drow .side{font-weight:700} .side.BUY{color:var(--buy)} .side.SELLu{color:var(--up)} .side.SELLd{color:var(--down)}
.drow .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drow .t b{font-weight:700;color:var(--ink)}
.drow .t{color:var(--mute)}
.stream{font:12px var(--sans);display:flex;align-items:center;gap:6px;color:var(--up)} .stream.off{color:var(--mute)}
.empty{color:var(--mute);font-size:12px;padding:6px 0}
.funnel{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12px;margin:2px 0 10px}
.funnel b{font:700 15px var(--sans)} .funnel i{color:var(--dim);font-style:normal}
.bar{display:flex;align-items:center;gap:8px;font-size:11px;margin:4px 0}
.bar .l{width:52%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mute)}
.bar .tr{flex:1;height:6px;background:var(--panel2);border-radius:3px;overflow:hidden}
.bar .f{display:block;height:100%;background:var(--filter)}
.bar .k{width:32px;text-align:right}
.pipeline{padding:6px 0 4px}
.agent{position:relative;display:grid;grid-template-columns:56px 1fr auto;gap:14px;align-items:center;padding:16px;border-radius:18px;
 background:linear-gradient(180deg,color-mix(in srgb,var(--c) 6%,var(--panel)),var(--panel));border:2px solid var(--c);
 box-shadow:0 0 18px -4px color-mix(in srgb,var(--c) 55%,transparent),inset 0 0 14px -8px var(--c);margin-bottom:26px}
.agent.off{opacity:.45} .agent.end::after{display:none}
.agent:not(:last-child)::after{content:"";position:absolute;left:50%;bottom:-28px;width:3px;height:26px;transform:translateX(-50%);
 background:linear-gradient(var(--c),var(--c2))}
.tile{width:56px;height:56px;border-radius:16px;display:grid;place-items:center;background:color-mix(in srgb,var(--c) 16%,#0b0f14)}
.agent .nm{font:800 17px var(--sans);letter-spacing:.8px;text-transform:uppercase}
.agent .ds{font:13px var(--sans);color:var(--mute);margin-top:2px}
.agent .last{font-size:11px;color:var(--dim);margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.agent .last b{color:var(--mute);font-weight:400}
.st{font:600 12px var(--mono);white-space:nowrap;align-self:center}
.st.ready{color:var(--up)} .st.working{color:var(--filter)} .st.error{color:var(--down)} .st.off{color:var(--dim)}
.agent.working .bod{animation:bob 1.1s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-1.5px)}}
.branch{color:var(--mute);font-size:11px;text-align:center;margin:-14px 0 12px}
table{width:100%;border-collapse:collapse;font-size:11px;font-variant-numeric:tabular-nums}
td,th{padding:6px 4px;border-top:1px solid var(--line);text-align:left;white-space:nowrap} th{color:var(--mute);font-weight:600;border-top:0}
td.r,th.r{text-align:right}
.verdict{font:14px/1.5 var(--sans)}
footer{color:var(--dim);font-size:10px;margin-top:16px;line-height:1.7}
[hidden]{display:none!important}
</style></head><body><main>
<header><div class="logo" id="logo"></div><h1>MemeDesk</h1><span class="pill" id="mode">…</span>
<span class="live"><span class="dot" id="dot"></span><span id="livetxt">…</span></span></header>
<div class="subline" id="subline">connecting…</div>
<div class="ticker"><div class="track" id="ticker"></div></div>
<section class="hero" id="hero">
 <div class="bal" id="bal">–</div><div id="dpill"></div>
 <div class="stats" id="stats"></div>
 <div class="pnlrow" id="pnlrow"></div>
 <div class="chart" id="chart"></div><div class="axis" id="axis"></div>
</section>
<nav class="chips" id="chips"></nav>
<nav class="tabs"><button data-tab="desk" class="on">Desk</button><button data-tab="pipeline">Pipeline</button><button data-tab="random">Random</button></nav>
<section data-pane="desk">
 <div class="card"><h2><span>positions · <span id="pcount">0</span> coins</span><small>entry → now</small></h2><div id="pos"></div></div>
 <div class="card"><h2><span>Desk core · decisions</span><span class="stream" id="stream"><span class="dot on"></span>streaming</span></h2><div id="dec"></div></div>
 <div class="card"><h2>funnel<small>last hour</small></h2><div id="funnel"></div></div>
</section>
<section data-pane="pipeline" hidden>
 <div class="pipeline" id="pipe"></div>
 <div class="card"><h2>tracked wallets<small id="wcount"></small></h2><div id="wallets"></div></div>
</section>
<section data-pane="random" hidden>
 <div class="card"><h2>scored vs random</h2><div class="verdict" id="verdict"></div></div>
 <div class="card"><h2><span>positions · <span id="rpcount">0</span> coins</span><small>entry → now</small></h2><div id="rpos"></div></div>
 <div class="card"><h2>random · decisions</h2><div id="rdec"></div></div>
</section>
<footer id="foot"></footer>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=v=>v==null?"–":(v<0?"−":"")+"$"+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const smoney=v=>v==null?"–":(v>=0?"+":"−")+"$"+Math.abs(v).toFixed(2);
const short$=v=>(v>=0?"+":"−")+"$"+(Math.abs(v)>=10?Math.round(Math.abs(v)):Math.abs(v).toFixed(2));
const pct=v=>v==null?"–":(v>=0?"+":"−")+Math.abs(v).toFixed(2)+"%";
const cls=v=>v==null?"mute":v>=0?"up":"down";
const px=v=>{if(v==null)return"–";v=+v;if(v>=1)return v.toFixed(4);const e=Math.floor(Math.log10(v));return v.toFixed(Math.min(12,Math.max(2,-e+3)))};
const mc=v=>!v?"–":v>=1e9?"$"+(v/1e9).toFixed(1)+"B":v>=1e6?"$"+(v/1e6).toFixed(1)+"M":v>=1e3?"$"+(v/1e3).toFixed(0)+"K":"$"+v.toFixed(0);
const ago=ts=>{if(!ts)return"never";const s=Date.now()/1e3-ts;return s<60?`${s|0}s`:s<3600?`${(s/60)|0}m`:s<172800?`${(s/3600).toFixed(1)}h`:`${(s/86400)|0}d`};
const hhmm=ts=>new Date(ts*1e3).toTimeString().slice(0,5);
const sym=s=>{s=String(s||"?");return s.startsWith("$")?s:"$"+s};
function hue(s){let h=0;for(const c of String(s))h=(h*31+c.charCodeAt(0))%360;return h}
function av(img,s,lg){const k=lg?" lg":"",bg=`hsl(${hue(s)} 60% 62%)`,ch=esc(String(s||"?").replace("$","").slice(0,1).toUpperCase());
 return`<span class="av${k}" style="background:${bg}">${ch}${img?`<img src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`:""}</span>`}
const ORDER=["whale_scout","scout","filter","analyst","risk","executor","logger","random"];
function bot(key,size){const c=key?`var(--${key})`:"#05070a",z=size||40; // original robot-blob, drawn for MemeDesk
 const mouth={whale_scout:"M15 26q5 3 10 0",scout:"M15 26h10",filter:"M15 27q5-3 10 0",analyst:"M16 26q4 3 8 0",risk:"M15 25l10 2",
  executor:"M15 26q5 3 10 0",logger:"M15 26h4m2 0h4",random:"M15 26q2.5-2 5 0t5 0"}[key]||"M15 26q5 3 10 0";
 return`<svg viewBox="0 0 40 40" width="${z}" height="${z}" aria-hidden="true"><g class="bod">
 <line x1="20" y1="4" x2="20" y2="10" stroke="${c}" stroke-width="2.2" stroke-linecap="round"/><circle cx="20" cy="4" r="2.6" fill="${c}"/>
 <path d="M7 22c0-8 5.5-12 13-12s13 4 13 12-5 12-13 12S7 30 7 22z" fill="${c}"/>
 <rect x="10.5" y="16.5" width="19" height="8" rx="4" fill="#0b0f14"/>
 <ellipse cx="16" cy="20.5" rx="1.9" ry="2.4" fill="#fff"/><ellipse cx="24" cy="20.5" rx="1.9" ry="2.4" fill="#fff"/>
 <path d="${mouth}" fill="none" stroke="#0b0f14" stroke-width="2" stroke-linecap="round" transform="translate(0 2)"/></g></svg>`}
$("logo").innerHTML=bot(null,30);
function chart(pts,book,start){
 const P=pts.filter(p=>p[book]!=null);
 if(P.length<2){$("axis").innerHTML="";return`<div class="empty" style="padding-top:60px;text-align:center">equity line appears after a few scan cycles</div>`}
 const W=500,H=150,vals=P.map(p=>p[book]).concat([start]);let lo=Math.min(...vals),hi=Math.max(...vals);const pad=(hi-lo)*.15||1;lo-=pad;hi+=pad;
 const t0=P[0].ts,t1=P[P.length-1].ts,x=t=>W*(t-t0)/(t1-t0||1),y=v=>H-H*(v-lo)/(hi-lo);
 const d=P.map((p,i)=>`${i?"L":"M"}${x(p.ts).toFixed(1)},${y(p[book]).toFixed(1)}`).join(""),last=P[P.length-1],col=last[book]>=start?"var(--up)":"var(--down)";
 $("axis").innerHTML=`<span>${hhmm(t0)}</span><span>— value &nbsp; -- put in</span><span>${hhmm(t1)}</span>`;
 return`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".35"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
 <line x1="0" x2="${W}" y1="${y(start)}" y2="${y(start)}" stroke="var(--dim)" stroke-width="1.5" stroke-dasharray="5 6" vector-effect="non-scaling-stroke"/>
 <path d="${d}L${x(t1)},${H}L0,${H}Z" fill="url(#g)"/><path d="${d}" fill="none" stroke="${col}" stroke-width="2.5" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>
 <span style="position:absolute;left:calc(${(100*x(t1)/W).toFixed(2)}% - 8px);top:${(y(last[book])-8).toFixed(1)}px;width:16px;height:16px;border-radius:50%;border:2.5px solid #e9eef3;background:var(--bg)"></span>`}
function positions(list){if(!list.length)return'<div class="empty">no open positions</div>';
 return list.map(p=>`<div class="prow">${av(p.image,p.symbol,1)}<div style="min-width:0"><div class="n"><a href="https://dexscreener.com/solana/${encodeURIComponent(p.pair)}" target="_blank" rel="noopener">${esc(sym(p.symbol))}</a><span>SOL · MC ${mc(p.mcap)}</span></div>
 <div class="s">${px(p.entry)} → ${px(p.now)}${p.tp_taken?" · TP taken":""} · ${p.age_h}h</div></div>
 <div class="v ${cls(p.pnl_usd)}">${smoney(p.pnl_usd)}<small>${pct(p.pnl_pct)}</small></div></div>`).join("")}
function decisions(list){if(!list.length)return'<div class="empty">no decisions yet</div>';
 return list.slice(0,40).map(d=>{const sc=d.side==="BUY"?"BUY":((d.pnl_usd??0)>=0?"SELLu":"SELLd");
  const amt=d.side==="BUY"&&d.usd!=null?` · ${money(d.usd)}`:"";
  return`<div class="drow"><span class="mute">${hhmm(d.ts)}</span><span class="side ${sc}">${esc(d.side)}</span>${av(d.image,d.symbol)}<span class="t"><b>${esc(sym(d.symbol))}</b> · ${esc(d.reason)}${amt}</span></div>`}).join("")}
let S=null,tab=(location.hash||"#desk").slice(1);
function setTab(t){tab=["desk","pipeline","random"].includes(t)?t:"desk";
 document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on",b.dataset.tab===tab));
 document.querySelectorAll("[data-pane]").forEach(p=>p.hidden=p.dataset.pane!==tab);
 history.replaceState(null,"","#"+tab);if(S)top_()}
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
addEventListener("hashchange",()=>setTab(location.hash.slice(1)));
function up_(){const s=S.now-S.started;return`${Math.floor(s/3600)}h ${String(Math.floor(s%3600/60)).padStart(2,"0")}m in`}
function top_(){
 const alive=S.now-S.heartbeat<Math.max(60,S.rules.monitor_s*4),nAg=ORDER.filter(k=>S.agents[k]&&S.agents[k].state!=="off").length;
 $("dot").className="dot "+(alive?"on":"bad");
 $("livetxt").textContent=!alive?"Stalled":tab==="pipeline"?`${nAg} agents`:"Live";
 $("mode").textContent=tab==="pipeline"?"pipeline":S.mode;
 const W=S.whale;
 $("subline").textContent=tab==="pipeline"?`whale pipeline · ${W.backend} · ${W.winners_scanned} winners read · ${W.winners_pending} queued`
  :`paper trading · ${S.profile} · ${S.wallets.length} whales tracked · ${up_()}`;
 const book=tab==="random"&&S.books.random?"random":"scored",b=S.books[book];
 $("hero").hidden=tab==="pipeline";
 const [w,c]=money(b.equity_usd).split(".");$("bal").innerHTML=`${esc(w)}<span class="c">.${esc(c||"00")}</span>`;
 $("dpill").innerHTML=b.delta_1h==null?"":`<span class="dpill ${cls(b.delta_1h)}" title="change over the last hour">${short$(b.delta_1h)}</span>`;
 $("stats").innerHTML=`<span class="box">in ${money(b.start_usd)} · ${b.entries} trade${b.entries===1?"":"s"}</span><span class="mult ${b.multiple<1?"down":""}">${b.multiple==null?"–":b.multiple.toFixed(2)+"×"}</span>${book==="random"?'<span class="box">random control</span>':""}`;
 $("pnlrow").innerHTML=`<span class="${cls(b.pnl_usd)}">${smoney(b.pnl_usd)} · ${b.wins}W ${b.losses}L</span><span class="mute">${hhmm(S.now)}</span>`;
 $("chart").innerHTML=chart(S.curve,book,b.start_usd)}
async function tick(){
 try{S=await (await fetch("/api/state",{cache:"no-store"})).json()}catch(e){$("dot").className="dot bad";$("livetxt").textContent="Offline";return}
 if(S.error){$("livetxt").textContent="error";return}
 const tk=S.ticker.map(t=>`<span class="tk">${av(t.image,t.symbol)}<b>${esc(sym(t.symbol))}</b><span class="${cls(t.h1)}">${t.h1==null?"–":pct(t.h1)}</span><span class="mute">1h</span></span>`).join("");
 $("ticker").innerHTML=tk?tk+tk:'<span class="tk mute">no tokens watched yet · waiting for the first scan</span>';
 $("ticker").style.setProperty("--dur",Math.max(25,S.ticker.length*5)+"s");
 top_();
 $("chips").innerHTML=ORDER.filter(k=>S.agents[k]).map(k=>{const a=S.agents[k];return`<span class="chip ${a.state}" style="--c:var(--${k})" data-k="${k}">${bot(k,18)}${esc(a.name)}</span>`}).join("");
 document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{setTab("pipeline");const el=$("ag-"+c.dataset.k);el&&el.scrollIntoView({behavior:"smooth",block:"center"})});
 const sc=S.books.scored,alive=S.now-S.heartbeat<Math.max(60,S.rules.monitor_s*4);
 $("pcount").textContent=sc.positions.length;$("pos").innerHTML=positions(sc.positions);
 $("stream").className="stream"+(alive?"":" off");$("stream").innerHTML=alive?'<span class="dot on"></span>streaming':'<span class="dot"></span>paused';
 $("dec").innerHTML=decisions(S.decisions.filter(d=>d.book==="scored"));
 const F=S.funnel,mx=Math.max(1,...F.reasons.map(r=>r.n));
 $("funnel").innerHTML=`<div class="funnel"><b>${F.scouted}</b> scouted <i>→</i> <b>${F.vetted}</b> safe <i>→</i> <b>${F.scored}</b> score ≥ ${S.rules.min_score} <i>→</i> <b>${F.bought}</b> bought</div>`
  +(F.reasons.length?'<div class="mute" style="font-size:11px;margin-bottom:4px">most common kills</div>'+F.reasons.map(r=>`<div class="bar"><span class="l">${esc(r.agent)} · ${esc(r.reason)}</span><span class="tr"><span class="f" style="width:${(100*r.n/mx).toFixed(0)}%"></span></span><span class="k">${r.n}</span></div>`).join(""):'<div class="empty">no kills in the last hour</div>');
 const ST={ready:"✓ ready",working:"● working",error:"✕ error",off:"off"},flow=ORDER.filter(k=>S.agents[k]&&k!=="random");
 const card=(k,nx)=>{const a=S.agents[k];return`<div class="agent ${a.state}${nx?"":" end"}" id="ag-${k}" style="--c:var(--${k});--c2:var(--${nx||k})"><div class="tile">${bot(k,40)}</div>
  <div style="min-width:0"><div class="nm" style="color:var(--ink)">${esc(a.name)}</div><div class="ds">${esc(a.desc)}</div>
  <div class="last">${a.last?`<b>${ago(a.ts)} ·</b> ${esc(a.last)}`:"no action yet"}</div></div><span class="st ${a.state}">${ST[a.state]||esc(a.state)}</span></div>`};
 $("pipe").innerHTML=flow.map((k,i)=>card(k,flow[i+1])).join("")+(S.agents.random?'<div class="branch">↳ branches off Filter, runs beside Analyst</div>'+card("random"):"");
 const W=S.whale;$("wcount").textContent=`${S.wallets.length} · ${W.backend}`;
 $("wallets").innerHTML=S.wallets.length?`<table><tr><th>wallet</th><th class="r">score</th><th class="r">wins</th><th class="r">pnl</th><th class="r">seen</th></tr>${S.wallets.slice(0,50).map(w=>`<tr><td><a href="https://solscan.io/account/${encodeURIComponent(w.address)}" target="_blank" rel="noopener">${esc(w.address.slice(0,4))}…${esc(w.address.slice(-4))}</a></td><td class="r">${w.score.toFixed(0)}</td><td class="r">${w.winners}</td><td class="r ${cls(w.pnl_sol)}">${w.pnl_sol==null?"?":(w.pnl_sol>=0?"+":"")+w.pnl_sol.toFixed(2)+"◎"}</td><td class="r mute">${ago(w.last_seen)}</td></tr>`).join("")}</table>`
  :`<div class="empty">none yet · last discovery ${ago(W.last_run)} ago · ${W.winners_pending} winners queued</div>`;
 if(S.books.random){const v=S.compare||{},r=S.books.random;
  $("verdict").innerHTML=`<div>${esc(v.verdict||"–")}</div><div class="mute" style="margin-top:6px;font:12px var(--mono)">scored ${r.avg_pct==null&&sc.avg_pct==null?"–":pct(sc.avg_pct)}/trade (n=${sc.closed}) · random ${pct(r.avg_pct)}/trade (n=${r.closed})${v.t!=null?` · t=${v.t}`:""}</div>`;
  $("rpcount").textContent=r.positions.length;$("rpos").innerHTML=positions(r.positions);$("rdec").innerHTML=decisions(S.decisions.filter(d=>d.book==="random"))}
 else $("verdict").textContent="Random control is off (DESK_RANDOM_CONTROL=false)";
 const R=S.rules,lc=S.last_cycle||{},o=lc.outcome||{};
 $("foot").innerHTML=`paper trading · ${R.size_pct}%/trade · stop −${R.stop}% · +${R.tp_trigger}% sells ${R.tp_sell}% · runner trails ${R.runner_trail}% (floor entry) · max ${R.max_hold_h}h<br>last scan ${ago(lc.ts)} ago · ${o.in_window??0} in window → ${o.vetted??0} vetted · http ${S.http.requests} req, ${S.http.rate_limited}×429, ${S.http.cache_hits} cached${S.coingecko.enabled?` · CoinGecko backup ${S.coingecko.used}/${S.coingecko.cap} this month`:""}`}
setTab(tab);tick();setInterval(tick,5000);
</script></body></html>
"""
