"""Local dashboard (stdlib http.server). Open http://127.0.0.1:8084 in the phone
browser while the desk runs in Termux. Read-only: it never places or changes trades."""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("memedesk.dashboard")


def build_state(desk) -> dict:
    L, cfg = desk.logger, desk.cfg
    s = L.summary(cfg.books)
    now = time.time()
    open_pos = [{
        "book": p.book, "symbol": p.symbol, "mint": p.mint, "pair": p.pair_address,
        "score": p.score, "cost": round(p.cost_usd, 2), "entry": p.entry_price,
        "last": p.last_price, "stop": p.stop_price, "tp_taken": p.tp_taken,
        "pnl_pct": round(p.unrealized_pct, 1), "age_h": round((now - p.opened_ts) / 3600, 1),
    } for p in L.positions("open")]
    closed = sorted(L.positions("closed"), key=lambda p: p.exit_ts or 0, reverse=True)[:40]
    closed_rows = [{
        "book": p.book, "symbol": p.symbol, "pair": p.pair_address, "score": p.score,
        "pnl_pct": round(p.pnl_pct or 0, 1), "pnl_usd": round(p.pnl_usd or 0, 2),
        "reason": p.exit_reason, "tp": p.tp_taken,
        "held_h": round(((p.exit_ts or now) - p.opened_ts) / 3600, 1), "exit_ts": p.exit_ts,
    } for p in closed]
    with L.lock:
        kills = L.db.execute("SELECT ts,agent,book,symbol,reason,detail FROM kills "
                             "ORDER BY ts DESC LIMIT 40").fetchall()
        curve_rows = L.db.execute("SELECT ts,data FROM events WHERE kind='cycle' "
                                  "ORDER BY ts DESC LIMIT 400").fetchall()
    curve = []
    for ts, data in reversed(curve_rows):
        try:
            curve.append({"ts": ts, **json.loads(data).get("equity", {})})
        except ValueError:
            pass
    return {
        "now": now, "started": desk.started, "books": cfg.books,
        "summary": s, "open": open_pos, "closed": closed_rows,
        "kills": [{"ts": t, "agent": a, "book": b, "symbol": sy, "reason": r, "detail": d}
                  for t, a, b, sy, r, d in kills],
        "curve": curve, "last_cycle": desk.last_cycle, "http": desk.http.stats,
        "rules": {"stop": cfg.stop_pct, "tp_trigger": cfg.tp_trigger_pct, "tp_sell": cfg.tp_sell_pct,
                  "runner_trail": cfg.runner_trail_pct, "max_hold_h": cfg.max_hold_h,
                  "size_pct": cfg.position_pct, "min_score": cfg.min_score,
                  "scan_s": cfg.scan_interval_s},
    }


def start_dashboard(desk) -> ThreadingHTTPServer | None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/state"):
                try:
                    body = json.dumps(build_state(desk), default=str).encode()
                except Exception as e:  # never take the desk down
                    log.exception("dashboard state failed")
                    body = json.dumps({"error": str(e)}).encode()
                ctype = "application/json"
            elif self.path in ("/", "/index.html"):
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
    log.warning("dashboard on http://%s:%s", desk.cfg.dashboard_host, desk.cfg.dashboard_port)
    return srv


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>memedesk</title>
<style>
:root{--bg:#0e1013;--panel:#161a1f;--line:#252b33;--ink:#e6e9ee;--mute:#8b94a1;
--up:#2fbf71;--down:#ef5a5a;--scored:#6aa8ff;--random:#c9a227;--warn:#e8a33d}
@media (prefers-color-scheme: light){:root{--bg:#f5f6f8;--panel:#fff;--line:#e3e6ea;--ink:#16191d;
--mute:#5d6672;--up:#178a4c;--down:#c93838;--scored:#2f6fd6;--random:#9a7a0c;--warn:#b26b00}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:12px 16px 40px}
header{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline;margin-bottom:12px}
h1{font-size:18px;margin:0;letter-spacing:.2px}
.tag{font-size:11px;font-weight:700;padding:2px 7px;border-radius:4px;background:var(--warn);color:#000}
.mute{color:var(--mute)} .up{color:var(--up)} .down{color:var(--down)}
.grid{display:grid;gap:12px;grid-template-columns:1fr 1fr}
@media (max-width:720px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mute);margin:0 0 8px;font-weight:600}
.book h2 span{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.big{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:6px 10px;margin-top:8px}
.kv div{font-size:12px;color:var(--mute)} .kv b{display:block;font-size:15px;color:var(--ink);font-variant-numeric:tabular-nums}
.verdict{margin:12px 0;font-weight:600}
.full{grid-column:1/-1}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:600;font-size:11px;text-transform:uppercase}
td.r,th.r{text-align:right}
.pill{font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--line)}
.pill.scored{color:var(--scored)} .pill.random{color:var(--random)}
.bar{display:flex;align-items:center;gap:8px;font-size:12px;margin:3px 0}
.bar .lbl{width:44%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .trk{flex:1;height:8px;background:var(--line);border-radius:4px;overflow:hidden}
.bar .fill{height:100%;background:var(--mute)}
.bar .n{width:42px;text-align:right;font-variant-numeric:tabular-nums}
svg{width:100%;height:140px;display:block}
a{color:inherit}
.empty{color:var(--mute);font-size:13px;padding:6px 0}
</style></head><body><main>
<header><h1>memedesk</h1><span class="tag">PAPER</span>
<span class="mute" id="meta">loading…</span></header>
<div class="card verdict" id="verdict">…</div>
<div class="grid" id="books"></div>
<div class="grid" style="margin-top:12px">
 <div class="card full"><h2>Equity by book</h2><div id="curve"></div></div>
 <div class="card full"><h2>Open positions</h2><div class="scroll" id="open"></div></div>
 <div class="card full"><h2>Closed trades</h2><div class="scroll" id="closed"></div></div>
 <div class="card"><h2>Kill reasons</h2><div id="reasons"></div></div>
 <div class="card"><h2>Recent kills</h2><div class="scroll" id="kills"></div></div>
</div>
<p class="mute" id="rules" style="font-size:12px;margin-top:14px"></p>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct=v=>v==null?"–":`<span class="${v>=0?"up":"down"}">${v>0?"+":""}${(+v).toFixed(1)}%</span>`;
const usd=v=>v==null?"–":`$${(+v).toLocaleString(undefined,{maximumFractionDigits:2})}`;
const px=v=>v==null?"–":(+v).toPrecision(4);
const ago=ts=>{if(!ts)return"–";const s=Date.now()/1000-ts;return s<90?`${s|0}s`:s<5400?`${(s/60)|0}m`:`${(s/3600).toFixed(1)}h`};
const pill=b=>`<span class="pill ${esc(b)}">${esc(b)}</span>`;
const dex=(p,t)=>`<a href="https://dexscreener.com/solana/${encodeURIComponent(p)}" target="_blank" rel="noopener">${esc(t)}</a>`;
function table(rows,cols){if(!rows.length)return'<div class="empty">none yet</div>';
 return`<table><tr>${cols.map(c=>`<th class="${c[2]||""}">${c[0]}</th>`).join("")}</tr>${rows.map(r=>`<tr>${cols.map(c=>`<td class="${c[2]||""}">${c[1](r)}</td>`).join("")}</tr>`).join("")}</table>`}
function curve(pts,books){if(pts.length<2)return'<div class="empty">needs a few scan cycles</div>';
 const W=1000,H=140,P=6,vals=pts.flatMap(p=>books.map(b=>p[b]).filter(v=>v!=null));
 let lo=Math.min(...vals),hi=Math.max(...vals);if(hi-lo<1){hi+=1;lo-=1}
 const t0=pts[0].ts,t1=pts[pts.length-1].ts||t0+1,x=t=>P+(W-2*P)*(t-t0)/(t1-t0||1),y=v=>H-P-(H-2*P)*(v-lo)/(hi-lo);
 const lines=books.map(b=>`<polyline fill="none" stroke="var(--${b})" stroke-width="2" vector-effect="non-scaling-stroke" points="${pts.filter(p=>p[b]!=null).map(p=>`${x(p.ts).toFixed(1)},${y(p[b]).toFixed(1)}`).join(" ")}"/>`).join("");
 return`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${lines}</svg><div class="mute" style="font-size:12px;display:flex;justify-content:space-between"><span>${usd(lo)}</span><span>${books.map(b=>`<span style="color:var(--${b})">●</span> ${b}`).join(" &nbsp; ")}</span><span>${usd(hi)}</span></div>`}
async function tick(){
 let s;try{s=await (await fetch("/api/state",{cache:"no-store"})).json()}catch(e){$("meta").textContent="desk not reachable";return}
 if(s.error){$("meta").textContent="error: "+s.error;return}
 const lc=s.last_cycle||{},o=lc.outcome||{};
 $("meta").innerHTML=`up ${ago(s.started)} · last scan ${ago(lc.ts)} ago (${lc.secs??"–"}s) · ${o.in_window??0} in window → ${o.vetted??0} vetted · http ${s.http.requests} req / ${s.http.rate_limited} 429s / ${s.http.cache_hits} cached`;
 const v=s.summary.scored_vs_random;
 $("verdict").innerHTML=v?`Scored vs random: ${esc(v.verdict)} <span class="mute">· diff ${v.diff_pct==null?"–":v.diff_pct+"%/trade"} · t=${v.t??"–"} · n=${v.n_scored}/${v.n_random}</span>`:"Random control is off";
 $("books").innerHTML=s.books.map(b=>{const k=s.summary.books[b];return`<div class="card book"><h2><span style="background:var(--${b})"></span>${b==="scored"?"Scored (analyst ≥ "+s.rules.min_score+")":"Random control"}</h2>
 <div class="big">${usd(k.equity_usd)} ${pct(k.return_pct)}</div>
 <div class="kv"><div>closed<b>${k.closed}</b></div><div>win rate<b>${k.win_rate??"–"}${k.win_rate==null?"":"%"}</b></div><div>avg / trade<b>${pct(k.avg_pct)}</b></div>
 <div>median<b>${pct(k.median_pct)}</b></div><div>realized<b>${usd(k.realized_usd)}</b></div><div>TP hits<b>${k.tp_hits}</b></div>
 <div>open<b>${k.open}</b></div><div>cash<b>${usd(k.cash_usd)}</b></div><div>entries<b>${k.entries}</b></div></div></div>`}).join("");
 $("curve").innerHTML=curve(s.curve,s.books);
 $("open").innerHTML=table(s.open,[["book",r=>pill(r.book)],["token",r=>dex(r.pair,r.symbol)],["score",r=>r.score??"–","r"],["cost",r=>usd(r.cost),"r"],["entry",r=>px(r.entry),"r"],["last",r=>px(r.last),"r"],["stop",r=>px(r.stop),"r"],["P&L",r=>pct(r.pnl_pct),"r"],["TP",r=>r.tp_taken?"✓":"",""],["age",r=>r.age_h+"h","r"]]);
 $("closed").innerHTML=table(s.closed,[["book",r=>pill(r.book)],["token",r=>dex(r.pair,r.symbol)],["score",r=>r.score??"–","r"],["P&L",r=>pct(r.pnl_pct),"r"],["$",r=>usd(r.pnl_usd),"r"],["exit",r=>esc(r.reason)],["TP",r=>r.tp?"✓":""],["held",r=>r.held_h+"h","r"],["when",r=>ago(r.exit_ts),"r"]]);
 const kr=Object.entries(s.summary.kills_by_reason).slice(0,14),mx=Math.max(1,...kr.map(e=>e[1]));
 $("reasons").innerHTML=kr.length?kr.map(([k,n])=>`<div class="bar"><span class="lbl" title="${esc(k)}">${esc(k)}</span><span class="trk"><span class="fill" style="display:block;width:${(100*n/mx).toFixed(1)}%"></span></span><span class="n">${n}</span></div>`).join(""):'<div class="empty">none yet</div>';
 $("kills").innerHTML=table(s.kills.slice(0,25),[["when",r=>ago(r.ts)],["agent",r=>esc(r.agent)+(r.book?" "+pill(r.book):"")],["token",r=>esc(r.symbol)],["reason",r=>esc(r.reason)],["detail",r=>`<span class="mute">${esc(r.detail)}</span>`]]);
 const R=s.rules;$("rules").textContent=`Rules: ${R.size_pct}% per trade · stop −${R.stop}% · at +${R.tp_trigger}% sell ${R.tp_sell}% · runner trails ${R.runner_trail}% from peak, never below entry · max hold ${R.max_hold_h}h · scan every ${R.scan_s}s · refreshes every 10s`;
}
tick();setInterval(tick,10000);
</script></body></html>
"""
