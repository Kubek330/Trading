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


def _book_view(desk, book: str, now: float) -> dict:
    L = desk.logger
    st = L.book_stats(book)
    st.pop("_returns", None)
    closed = L.positions("closed", book)
    wins = sum(1 for p in closed if (p.pnl_usd or 0) > 0)
    quotes = desk.risk.quotes
    positions = []
    for p in L.positions("open", book):
        q = quotes.get(p.mint, {})
        positions.append({
            "symbol": p.symbol, "mint": p.mint, "pair": p.pair_address, "score": p.score,
            "mcap": q.get("mcap"), "entry": p.entry_price, "now": p.last_price,
            "pnl_usd": round(p.realized_usd + p.value_usd - p.cost_usd, 2),
            "pnl_pct": round(p.unrealized_pct, 1), "tp_taken": p.tp_taken,
            "stop": p.stop_price, "age_h": round((now - p.opened_ts) / 3600, 1)})
    return {**st, "wins": wins, "losses": len(closed) - wins, "positions": positions,
            "pnl_usd": round(st["equity_usd"] - st["start_usd"], 2),
            "multiple": round(st["equity_usd"] / st["start_usd"], 3) if st["start_usd"] else None}


def build_state(desk) -> dict:
    L, cfg, now = desk.logger, desk.cfg, time.time()
    with L.lock:
        dec_rows = L.db.execute("SELECT ts,symbol,mint,data FROM events WHERE kind='decision' "
                                "ORDER BY ts DESC LIMIT 80").fetchall()
        curve_rows = L.db.execute("SELECT ts,data FROM events WHERE kind='cycle' "
                                  "ORDER BY ts DESC LIMIT 720").fetchall()
    decisions = []
    for ts, sym, mint, data in dec_rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        decisions.append({"ts": ts, "symbol": sym, "mint": mint, "side": d.get("side"),
                          "book": d.get("book"), "reason": d.get("reason"), "origin": d.get("origin")})
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
        if not w.get("symbol") and q.get("symbol"):
            w["symbol"] = q["symbol"]
    ticker = sorted(watch.values(), key=lambda w: -w["ts"])[:30]
    tracked = L.tracked_wallets()
    cmp = L.summary(cfg.books)["scored_vs_random"]
    agents = {k: {**v, "name": AGENT_INFO[k][0], "desc": AGENT_INFO[k][1]} for k, v in desk.agents.items()}
    return {
        "now": now, "started": desk.started, "heartbeat": desk.heartbeat, "mode": desk.mode,
        "books": {b: _book_view(desk, b, now) for b in cfg.books}, "compare": cmp,
        "curve": curve, "decisions": decisions, "ticker": ticker, "agents": agents,
        "wallets": [{"address": w["address"], "score": w["score"], "winners": len(w["winners"]),
                     "pnl_sol": w["pnl_sol"], "last_seen": w["last_seen"]} for w in tracked],
        "whale": {"backend": desk.chain.backend, "last_run": desk.whale.last_run,
                  "last_watch": desk.whale.last_watch, "winners_scanned": len(L.winners("scanned")),
                  "winners_pending": len(L.winners("pending"))},
        "last_cycle": desk.last_cycle, "http": desk.http.stats,
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
<meta name="theme-color" content="#06080b">
<title>MemeDesk</title>
<style>
:root{--bg:#06080b;--panel:#0c1015;--panel2:#10161d;--line:#1a222c;--ink:#d7e1ea;--mute:#6c7c8d;--dim:#3a4653;
--up:#3ddc84;--down:#ff5f5f;--whale_scout:#4cc9f0;--scout:#7bd88f;--filter:#ffb454;--analyst:#c792ea;
--risk:#ff6b6b;--executor:#ffd166;--logger:#8fa6ba;--random:#f78fb3;
--mono:ui-monospace,"JetBrains Mono","SFMono-Regular",Menlo,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:13px/1.45 var(--mono);-webkit-font-smoothing:antialiased}
main{max-width:560px;margin:0 auto;padding:10px 16px 48px}
a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--dim)}
.mute{color:var(--mute)} .up{color:var(--up)} .down{color:var(--down)}
header{display:flex;align-items:center;gap:8px;padding:6px 0 10px}
header h1{font-size:17px;margin:0;letter-spacing:.5px;font-weight:700}
header h1 b{color:var(--up)}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mute);white-space:nowrap}
.pill.mode{color:var(--whale_scout);border-color:color-mix(in srgb,var(--whale_scout) 45%,transparent)}
.pill.mode.pipe{color:var(--scout);border-color:color-mix(in srgb,var(--scout) 45%,transparent)}
.live{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--up);box-shadow:0 0 0 0 rgba(61,220,132,.6);animation:pulse 2s infinite}
.dot.bad{background:var(--down)}
@keyframes pulse{70%{box-shadow:0 0 0 7px rgba(61,220,132,0)}100%{box-shadow:0 0 0 0 rgba(61,220,132,0)}}
.ticker{overflow:hidden;border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin:0 -16px;
 padding:7px 0;white-space:nowrap;mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent)}
.ticker .track{display:inline-block;padding-left:16px;animation:tick var(--dur,40s) linear infinite}
.ticker span.t{margin-right:22px;font-size:12px}
@keyframes tick{to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.ticker{overflow-x:auto}.ticker .track{animation:none}.dot.on{animation:none}}
.hero{padding:18px 0 6px}
.hero .lbl{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:1px}
.bal{font-size:44px;font-weight:700;letter-spacing:-1px;line-height:1.1;margin:4px 0;font-variant-numeric:tabular-nums}
.sub{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;color:var(--mute);font-size:12px}
.mult{font-weight:700;font-size:12px;padding:1px 7px;border-radius:6px}
.mult.up{background:rgba(61,220,132,.12)} .mult.down{background:rgba(255,95,95,.12)}
.chart{margin:12px 0 4px;height:120px;position:relative}
.chart svg{width:100%;height:100%;display:block;overflow:visible}
.chips{display:flex;gap:6px;overflow-x:auto;padding:10px 0;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;display:flex;align-items:center;gap:6px;font-size:11px;padding:4px 9px;border-radius:99px;
 border:1px solid color-mix(in srgb,var(--c) 40%,transparent);color:var(--c);background:color-mix(in srgb,var(--c) 7%,transparent);cursor:pointer}
.chip i{width:6px;height:6px;border-radius:50%;background:var(--c)}
.chip.working i{animation:blink 1s infinite} .chip.error i{background:var(--down)} .chip.off{opacity:.4}
@keyframes blink{50%{opacity:.2}}
.tabs{display:flex;border-bottom:1px solid var(--line);margin:6px 0 12px}
.tabs button{flex:1;background:none;border:0;color:var(--mute);font:600 12px var(--mono);padding:10px 0;
 border-bottom:2px solid transparent;cursor:pointer;letter-spacing:.5px}
.tabs button.on{color:var(--ink);border-color:var(--up)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px}
.card h2{margin:0 0 8px;font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:1px;font-weight:600;display:flex;justify-content:space-between}
.row{display:grid;grid-template-columns:1fr auto;gap:2px 10px;padding:8px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.row .tk{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .r{text-align:right;font-variant-numeric:tabular-nums}
.row .s{font-size:11px;color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dec{display:grid;grid-template-columns:42px 40px minmax(0,72px) 1fr;gap:8px;align-items:baseline;padding:6px 0;border-top:1px solid var(--line);font-size:12px}
.dec:first-of-type{border-top:0}
.dec .side{font-weight:700;font-size:11px}
.dec .side.BUY{color:var(--up)} .dec .side.SELL{color:var(--down)}
.dec .tk{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dec .why{color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{color:var(--mute);font-size:12px;padding:4px 0}
.pipe{position:relative;padding-left:0}
.agent{position:relative;display:grid;grid-template-columns:44px 1fr;gap:10px;align-items:center;
 background:var(--panel);border:1px solid color-mix(in srgb,var(--c) 55%,transparent);border-radius:12px;padding:10px 12px;margin-bottom:18px}
.agent:not(:last-child)::after{content:"";position:absolute;left:33px;bottom:-19px;width:2px;height:18px;
 background:linear-gradient(var(--c),var(--c2,var(--line)))}
.agent .nm{font-weight:700;color:var(--c);display:flex;justify-content:space-between;gap:8px}
.agent .ds{font-size:11px;color:var(--mute)}
.agent .last{font-size:11px;margin-top:4px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st{font-size:11px;font-weight:600;white-space:nowrap}
.st.ready{color:var(--up)} .st.working{color:var(--executor)} .st.error{color:var(--down)} .st.off{color:var(--dim)}
.agent.working svg .bod{animation:bob 1.2s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-1.5px)}}
.side-note{font-size:11px;color:var(--mute);margin:-8px 0 14px 4px}
.verdict{font-size:12px;line-height:1.5}
table{width:100%;border-collapse:collapse;font-size:11px;font-variant-numeric:tabular-nums}
td,th{padding:5px 4px;border-top:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--mute);font-weight:600;border-top:0}
td.r,th.r{text-align:right}
footer{color:var(--dim);font-size:10px;margin-top:16px;line-height:1.6}
[hidden]{display:none!important}
</style></head><body><main>
<header><h1>Meme<b>Desk</b></h1><span class="pill mode" id="mode">…</span><span class="pill">paper</span>
<span class="live"><span class="dot" id="dot"></span><span id="livetxt">…</span></span></header>
<div class="ticker"><div class="track" id="ticker"></div></div>
<section class="hero">
 <div class="lbl" id="herolbl">Scored book</div>
 <div class="bal" id="bal">–</div>
 <div class="sub" id="sub1"></div>
 <div class="sub" id="sub2" style="margin-top:4px"></div>
 <div class="chart" id="chart"></div>
</section>
<nav class="chips" id="chips"></nav>
<nav class="tabs"><button data-tab="desk" class="on">Desk</button><button data-tab="pipeline">Pipeline</button><button data-tab="random">Random</button></nav>
<section data-pane="desk">
 <div class="card"><h2><span>Positions</span><span id="poscount"></span></h2><div id="pos"></div></div>
 <div class="card"><h2><span>Decisions</span></h2><div id="dec"></div></div>
</section>
<section data-pane="pipeline" hidden>
 <div class="pipe" id="pipe"></div>
 <div class="card"><h2><span>Tracked wallets</span><span id="wcount"></span></h2><div id="wallets"></div></div>
</section>
<section data-pane="random" hidden>
 <div class="card"><h2><span>Scored vs random</span></h2><div class="verdict" id="verdict"></div></div>
 <div class="card"><h2><span>Random positions</span><span id="rposcount"></span></h2><div id="rpos"></div></div>
 <div class="card"><h2><span>Random decisions</span></h2><div id="rdec"></div></div>
</section>
<footer id="foot"></footer>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=v=>v==null?"–":(v<0?"−":"")+"$"+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const smoney=v=>v==null?"–":(v>=0?"+":"−")+"$"+Math.abs(v).toFixed(2);
const pct=v=>v==null?"–":(v>=0?"+":"−")+Math.abs(v).toFixed(1)+"%";
const cls=v=>v==null?"mute":v>=0?"up":"down";
const px=v=>{if(v==null)return"–";v=+v;if(v>=1)return v.toFixed(4);const e=Math.floor(Math.log10(v));return v.toFixed(Math.min(12,Math.max(2,-e+3)))};
const mc=v=>!v?"–":v>=1e9?"$"+(v/1e9).toFixed(2)+"B":v>=1e6?"$"+(v/1e6).toFixed(2)+"M":v>=1e3?"$"+(v/1e3).toFixed(0)+"K":"$"+v.toFixed(0);
const ago=ts=>{if(!ts)return"never";const s=Date.now()/1e3-ts;return s<60?`${s|0}s`:s<3600?`${(s/60)|0}m`:s<172800?`${(s/3600).toFixed(1)}h`:`${(s/86400)|0}d`};
const hhmm=ts=>{const d=new Date(ts*1e3);return d.toTimeString().slice(0,5)};
const dex=(pair,txt)=>pair?`<a href="https://dexscreener.com/solana/${encodeURIComponent(pair)}" target="_blank" rel="noopener">${esc(txt)}</a>`:esc(txt);
const ORDER=["whale_scout","scout","filter","analyst","risk","executor","logger","random"];
const FACES={whale_scout:"w",scout:"o",filter:"f",analyst:"a",risk:"r",executor:"e",logger:"l",random:"q"};
function bot(key){ // original round robot-blob: antenna, soft body, two eyes, per-agent mouth
 const c=`var(--${key})`,m={w:"M14 26q6 4 12 0",o:"M15 26h10",f:"M15 27q5-3 10 0",a:"M16 26q4 3 8 0",r:"M15 25l10 2",e:"M15 26q5 3 10 0",l:"M15 26h4m2 0h4",q:"M15 26q2.5-2 5 0t5 0"}[FACES[key]];
 return `<svg viewBox="0 0 40 40" width="40" height="40" aria-hidden="true"><g class="bod">
 <line x1="20" y1="4" x2="20" y2="10" stroke="${c}" stroke-width="2" stroke-linecap="round"/><circle cx="20" cy="4" r="2.6" fill="${c}"/>
 <path d="M7 22c0-8 5.5-12 13-12s13 4 13 12-5 12-13 12S7 30 7 22z" fill="${c}" fill-opacity=".16" stroke="${c}" stroke-width="2"/>
 <circle cx="15" cy="20" r="3.2" fill="#fff"/><circle cx="25" cy="20" r="3.2" fill="#fff"/>
 <circle cx="15.8" cy="20.6" r="1.5" fill="#06080b"/><circle cx="25.8" cy="20.6" r="1.5" fill="#06080b"/>
 <path d="${m}" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round"/>
 <circle cx="9.5" cy="25" r="1.6" fill="${c}" fill-opacity=".35"/><circle cx="30.5" cy="25" r="1.6" fill="${c}" fill-opacity=".35"/></g></svg>`}
function chart(pts,book,start){
 const P=pts.filter(p=>p[book]!=null);if(P.length<2)return`<div class="empty" style="padding-top:44px;text-align:center">equity line appears after a few scan cycles</div>`;
 const W=500,H=120,vals=P.map(p=>p[book]).concat([start]);let lo=Math.min(...vals),hi=Math.max(...vals);const pad=(hi-lo)*.12||1;lo-=pad;hi+=pad;
 const t0=P[0].ts,t1=P[P.length-1].ts,x=t=>(W*(t-t0)/(t1-t0||1)),y=v=>H-(H*(v-lo)/(hi-lo));
 const d=P.map((p,i)=>`${i?"L":"M"}${x(p.ts).toFixed(1)},${y(p[book]).toFixed(1)}`).join(""),last=P[P.length-1][book],col=last>=start?"var(--up)":"var(--down)";
 return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".25"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
 <line x1="0" x2="${W}" y1="${y(start)}" y2="${y(start)}" stroke="var(--mute)" stroke-dasharray="4 5" vector-effect="non-scaling-stroke"/>
 <path d="${d}L${W},${H}L0,${H}Z" fill="url(#g)"/><path d="${d}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>
 <div class="mute" style="position:absolute;left:0;top:${Math.max(0,Math.min(H-14,y(start)-15))}px;font-size:10px">put in ${money(start)}</div>`}
function positions(list){if(!list.length)return'<div class="empty">no open positions</div>';
 return list.map(p=>`<div class="row"><div class="tk">${dex(p.pair,p.symbol)} <span class="mute" style="font-weight:400">· MC ${mc(p.mcap)}</span></div>
 <div class="r ${cls(p.pnl_usd)}" style="font-weight:700">${smoney(p.pnl_usd)}</div>
 <div class="s">${px(p.entry)} → ${px(p.now)}${p.tp_taken?" · TP taken":""} · ${p.age_h}h</div><div class="r s ${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</div></div>`).join("")}
function decisions(list){if(!list.length)return'<div class="empty">no decisions yet</div>';
 return list.slice(0,40).map(d=>`<div class="dec"><span class="mute">${hhmm(d.ts)}</span><span class="side ${d.side==="BUY"?"BUY":"SELL"}">${esc(d.side)}</span><span class="tk">${esc(d.symbol)}</span><span class="why" title="${esc(d.reason)}">${esc(d.reason)}</span></div>`).join("")}
let S=null,tab=(location.hash||"#desk").slice(1);
function setTab(t){tab=["desk","pipeline","random"].includes(t)?t:"desk";
 document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on",b.dataset.tab===tab));
 document.querySelectorAll("[data-pane]").forEach(p=>p.hidden=p.dataset.pane!==tab);
 history.replaceState(null,"","#"+tab);if(S)hero()}
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
addEventListener("hashchange",()=>setTab(location.hash.slice(1)));
function hero(){
 const book=tab==="random"&&S.books.random?"random":"scored",b=S.books[book];
 $("herolbl").textContent=book==="random"?"Random control book":"Scored book";
 $("bal").innerHTML=money(b.equity_usd);
 $("sub1").innerHTML=`<span>in ${money(b.start_usd)} · ${b.entries} trade${b.entries===1?"":"s"}</span><span class="mult ${cls(b.multiple-1)}">${b.multiple==null?"–":b.multiple.toFixed(2)+"x"}</span>`;
 $("sub2").innerHTML=`<span class="${cls(b.pnl_usd)}">${smoney(b.pnl_usd)}</span><span>· ${b.wins} win${b.wins===1?"":"s"} ${b.losses} loss${b.losses===1?"":"es"}</span>${b.open?`<span>· ${b.open} open</span>`:""}`;
 $("chart").innerHTML=chart(S.curve,book,b.start_usd)}
async function tick(){
 try{S=await (await fetch("/api/state",{cache:"no-store"})).json()}catch(e){$("dot").className="dot bad";$("livetxt").textContent="Offline";return}
 if(S.error){$("livetxt").textContent="error";return}
 const alive=S.now-S.heartbeat<Math.max(60,S.rules.monitor_s*4);
 $("dot").className="dot "+(alive?"on":"bad");$("livetxt").textContent=alive?"Live":"Stalled";
 $("mode").textContent=S.mode;$("mode").className="pill mode"+(S.mode==="whale mode"?"":" pipe");
 const tk=S.ticker.map(t=>`<span class="t"><b>${esc(t.symbol)}</b> <span class="${cls(t.h1)}">${t.h1==null?"–":pct(t.h1)}</span></span>`).join("");
 $("ticker").innerHTML=tk?tk+tk:'<span class="t mute">no tokens watched yet · waiting for the first scan</span>';
 $("ticker").style.setProperty("--dur",Math.max(20,S.ticker.length*4)+"s");
 hero();
 $("chips").innerHTML=ORDER.filter(k=>S.agents[k]).map(k=>{const a=S.agents[k];return`<span class="chip ${a.state}" style="--c:var(--${k})" data-k="${k}"><i></i>${esc(a.name)}</span>`}).join("");
 document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{setTab("pipeline");const el=$("ag-"+c.dataset.k);el&&el.scrollIntoView({behavior:"smooth",block:"center"})});
 const sc=S.books.scored;$("poscount").textContent=sc.positions.length?sc.positions.length+" open":"";
 $("pos").innerHTML=positions(sc.positions);
 $("dec").innerHTML=decisions(S.decisions.filter(d=>d.book==="scored"));
 const ST={ready:"✓ ready",working:"● working",error:"✕ error",off:"off"};
 const flow=ORDER.filter(k=>S.agents[k]&&k!=="random");
 $("pipe").innerHTML=flow.map((k,i)=>{const a=S.agents[k],nx=flow[i+1];return`<div class="agent ${a.state}" id="ag-${k}" style="--c:var(--${k});--c2:var(--${nx||k})">${bot(k)}<div style="min-width:0">
 <div class="nm"><span>${esc(a.name)}</span><span class="st ${a.state}">${ST[a.state]||esc(a.state)}</span></div><div class="ds">${esc(a.desc)}</div>
 <div class="last">${a.last?`<span class="mute">${ago(a.ts)} ·</span> ${esc(a.last)}`:'<span class="mute">no action yet</span>'}</div></div></div>`}).join("")
 +(S.agents.random?(()=>{const a=S.agents.random;return`<div class="side-note">↳ branches off Filter, in parallel with Analyst</div><div class="agent ${a.state}" id="ag-random" style="--c:var(--random)">${bot("random")}<div style="min-width:0"><div class="nm"><span>${esc(a.name)}</span><span class="st ${a.state}">${ST[a.state]||esc(a.state)}</span></div><div class="ds">${esc(a.desc)}</div><div class="last">${a.last?`<span class="mute">${ago(a.ts)} ·</span> ${esc(a.last)}`:'<span class="mute">no action yet</span>'}</div></div></div>`})():"");
 const W=S.whale;$("wcount").textContent=`${S.wallets.length} · ${W.winners_scanned} winners read · ${W.backend}`;
 $("wallets").innerHTML=S.wallets.length?`<table><tr><th>wallet</th><th class="r">score</th><th class="r">wins</th><th class="r">pnl</th><th class="r">seen</th></tr>${S.wallets.slice(0,50).map(w=>`<tr><td><a href="https://solscan.io/account/${encodeURIComponent(w.address)}" target="_blank" rel="noopener">${esc(w.address.slice(0,4))}…${esc(w.address.slice(-4))}</a></td><td class="r">${w.score.toFixed(0)}</td><td class="r">${w.winners}</td><td class="r ${cls(w.pnl_sol)}">${w.pnl_sol==null?"?":(w.pnl_sol>=0?"+":"")+w.pnl_sol.toFixed(2)+"◎"}</td><td class="r mute">${ago(w.last_seen)}</td></tr>`).join("")}</table>`
   :`<div class="empty">none yet · last discovery ${ago(W.last_run)} ago · ${W.winners_pending} winners queued</div>`;
 if(S.books.random){const v=S.compare||{},r=S.books.random;
  $("verdict").innerHTML=`<div>${esc(v.verdict||"–")}</div><div class="mute" style="margin-top:6px">scored ${pct(sc.avg_pct)}/trade (n=${sc.closed}) · random ${pct(r.avg_pct)}/trade (n=${r.closed})${v.t!=null?` · t=${v.t}`:""}</div>`;
  $("rposcount").textContent=r.positions.length?r.positions.length+" open":"";
  $("rpos").innerHTML=positions(r.positions);$("rdec").innerHTML=decisions(S.decisions.filter(d=>d.book==="random"))}
 else $("verdict").textContent="Random control is off (DESK_RANDOM_CONTROL=false)";
 const R=S.rules,lc=S.last_cycle||{},o=lc.outcome||{};
 $("foot").innerHTML=`paper trading · ${R.size_pct}%/trade · stop −${R.stop}% · +${R.tp_trigger}% sells ${R.tp_sell}% · runner trails ${R.runner_trail}% (floor entry) · max ${R.max_hold_h}h<br>last scan ${ago(lc.ts)} ago · ${o.in_window??0} in window → ${o.vetted??0} vetted · http ${S.http.requests} req, ${S.http.rate_limited}×429, ${S.http.cache_hits} cached`}
setTab(tab);tick();setInterval(tick,5000);
</script></body></html>
"""
