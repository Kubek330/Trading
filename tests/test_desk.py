import copy
import threading
import time

import pytest

from memedesk.agents.risk import step, runner_stop
from memedesk.agents.analyst import score_momentum, score_unique_buyers, lin
from memedesk.config import Config
from memedesk.desk import Desk
from memedesk.http import DataError
from memedesk.models import HandoffError, Position, RiskApproval, Vetted

NOW = time.time()
GOOD = "GoodMint1111111111111111111111111111111111"
CREATOR = "Creator11111111111111111111111111111111111"


def pair(mint=GOOD, sym="GOOD", age_h=5.0, mcap=1_000_000, liq=300_000, h1=20.0,
         m5=2.0, h6=40.0, price=0.001, buys=900, sells=400):
    return {
        "chainId": "solana", "dexId": "pumpswap", "pairAddress": f"pool_{mint}",
        "baseToken": {"address": mint, "symbol": sym, "name": sym},
        "quoteToken": {"symbol": "SOL"}, "priceUsd": str(price),
        "txns": {"h1": {"buys": buys, "sells": sells}},
        "volume": {"h1": 250_000}, "priceChange": {"m5": m5, "h1": h1, "h6": h6},
        "liquidity": {"usd": liq}, "marketCap": mcap,
        "pairCreatedAt": int((NOW - age_h * 3600) * 1000),
    }


def report(**over):
    r = {
        "creator": CREATOR, "creatorBalance": 0,
        "token": {"supply": 1_000_000_000, "mintAuthority": None, "freezeAuthority": None},
        "markets": [{"pubkey": f"pool_{GOOD}", "liquidityAAccount": "PoolVault",
                     "lp": {"lpLockedPct": 100, "quoteUSD": 150000}}],
        "topHolders": [{"address": "PoolVault", "owner": f"pool_{GOOD}", "pct": 22.0},
                       {"address": "a1", "owner": "SmartA", "pct": 1.2, "insider": False}]
                      + [{"address": f"h{i}", "owner": f"o{i}", "pct": 1.5} for i in range(12)],
        "risks": [], "totalHolders": 2500, "rugged": False,
    }
    r.update(over)
    return r


class FakeDex:
    def __init__(self, pairs):
        self.pairs = {p["baseToken"]["address"]: p for p in pairs}

    def discover_mints(self):
        return list(self.pairs)

    def pairs_for_mints(self, mints):
        return [self.pairs[m] for m in mints if m in self.pairs]

    from memedesk.sources.dexscreener import DexScreener as _D
    best_pair_per_mint = staticmethod(_D.best_pair_per_mint)

    def prices(self, mints):
        return {m: {"price": float(self.pairs[m]["priceUsd"]),
                    "liq": self.pairs[m]["liquidity"]["usd"], "mcap": self.pairs[m]["marketCap"],
                    "h1": self.pairs[m]["priceChange"]["h1"], "symbol": self.pairs[m]["baseToken"]["symbol"],
                    "pair": self.pairs[m]}
                for m in mints if m in self.pairs}

    def set_price(self, mint, price, liq=None):
        self.pairs[mint]["priceUsd"] = str(price)
        if liq is not None:
            self.pairs[mint]["liquidity"]["usd"] = liq


class FakeGecko:
    def __init__(self, n_buyers=140, smart=("SmartB",)):
        self.n, self.smart = n_buyers, smart

    def discover_mints(self):
        return []

    def trades(self, pool, window_min=60):
        t = [{"ts": NOW, "kind": "buy", "wallet": f"w{i}", "usd": 120.0} for i in range(self.n)]
        t += [{"ts": NOW, "kind": "buy", "wallet": w, "usd": 500.0} for w in self.smart]
        t += [{"ts": NOW, "kind": "sell", "wallet": f"s{i}", "usd": 100.0} for i in range(60)]
        return t


class FakeRPC:
    def __init__(self, mint_auth=None, freeze_auth=None, fail=False):
        self.m, self.f, self.fail = mint_auth, freeze_auth, fail

    def mint_info(self, mint):
        if self.fail:
            raise DataError("rpc down")
        return {"mint_authority": self.m, "freeze_authority": self.f, "supply": 10**9, "decimals": 6}


class FakeRug:
    def __init__(self, rep):
        self.rep = rep

    def report(self, mint):
        if self.rep is None:
            raise DataError("rugcheck down")
        return copy.deepcopy(self.rep)


class FakeBundles:
    def __init__(self, pct=2.0):
        self.pct = pct

    def bundle_pct(self, mint, rep):
        return (self.pct, "test") if self.pct is not None else (None, None)


class FakeJup:
    def __init__(self, score=80.0, label="high"):
        self.score, self.label = score, label

    def prefetch(self, mints):
        pass

    def organic(self, mint):
        return None if self.score is None else {"score": self.score, "label": self.label, "verified": False}


class NoGrok:
    enabled = False

    def morning_report(self, s):
        return ""


def make_desk(tmp_path, pairs=None, rep=None, rpc=None, bundles=None, gecko=None, jup=None, **cfg_over):
    cfg_over.setdefault("random_seed", 7)
    cfg_over.setdefault("profile", "strict")
    cfg = Config(data_dir=str(tmp_path), **cfg_over)
    cfg.validate()
    dex = FakeDex(pairs or [pair()])
    d = Desk(cfg, dex=dex, gecko=gecko or FakeGecko(), rpc=rpc or FakeRPC(),
             rug=FakeRug(report() if rep is None else rep), bundles=bundles or FakeBundles(),
             grok=NoGrok(), smart_wallets={"SmartA", "SmartB"}, jup=jup or FakeJup())
    # pre-seed a holder snapshot 30 min ago so holder growth is measurable
    d.logger.db.execute("INSERT INTO holders(mint,ts,holders) VALUES(?,?,?)", (GOOD, time.time() - 1800, 2000))
    d.logger.db.commit()
    return d, dex


def kills(d, book=None):
    q = "SELECT reason FROM kills" + (" WHERE book=?" if book is not None else "")
    return [r[0] for r in d.logger.db.execute(q, (book,) if book is not None else ())]


# ---------------------------------------------------------------- pipeline

def test_good_token_enters_both_books_with_stop_and_8pct_cap(tmp_path):
    d, _ = make_desk(tmp_path)
    out = d.scan_cycle()
    assert out.get("scored_entered") == 1 and out.get("random_entered") == 1, kills(d)
    for book in ("scored", "random"):
        [p] = d.logger.positions("open", book)
        assert p.cost_usd == pytest.approx(80.0)                 # 8% of the book's $1000
        assert p.stop_price == pytest.approx(p.entry_price * 0.85)  # -15%, set at entry
        assert d.broker.cash_usd(book) == pytest.approx(920.0)
    assert d.logger.positions("open", "random")[0].score is None
    trail = [r[0] for r in d.logger.db.execute("SELECT reason FROM events WHERE kind='handoff'")]
    assert trail[:5] == ["scout->filter", "filter->analyst", "analyst->executor",
                         "executor->risk", "risk->executor"]
    assert trail[5:] == ["random->executor", "executor->risk", "risk->executor"]


def test_config_cannot_raise_position_above_8pct(tmp_path):
    d, _ = make_desk(tmp_path, position_pct=25)
    d.scan_cycle()
    assert all(p.cost_usd <= 80.0 + 1e-6 for p in d.logger.positions())


@pytest.mark.parametrize("kw,reason", [
    (dict(pairs=[pair(age_h=0.2)]), "too_young"),
    (dict(pairs=[pair(age_h=80)]), "too_old"),
    (dict(pairs=[pair(mcap=200_000, liq=60_000)]), "mcap_low"),
    (dict(pairs=[pair(mcap=25_000_000, liq=8_000_000)]), "mcap_high"),
    (dict(pairs=[pair(h1=95)]), "pumped_1h"),
    (dict(pairs=[pair(liq=40_000)]), "liq_thin"),                       # 4% < 5%
    (dict(rpc=FakeRPC(mint_auth="Auth111111")), "mint_authority_live"),
    (dict(rpc=FakeRPC(freeze_auth="Frz1111111")), "freeze_authority_live"),
    (dict(rep=report(markets=[{"pubkey": f"pool_{GOOD}", "lp": {"lpLockedPct": 40}}])), "lp_unlocked"),
    (dict(rep=report(creatorBalance=50_000_000)), "dev_heavy"),
    (dict(rep=report(risks=[{"name": "Creator history of rugged tokens"}])), "dev_past_rugs"),
    (dict(rep=report(topHolders=[{"address": f"w{i}", "owner": f"o{i}", "pct": 3.0} for i in range(10)])),
     "top10_heavy"),                                                     # 30% > 25%
    (dict(rep=report(topHolders=[])), "unknown_top10"),
    (dict(bundles=FakeBundles(14.0)), "bundled_launch"),
    (dict(bundles=FakeBundles(None)), "unknown_bundle"),
    (dict(rep=report(rugged=True)), "rugged"),
])
def test_filter_kills(tmp_path, kw, reason):
    d, _ = make_desk(tmp_path, **kw)
    d.scan_cycle()
    assert d.logger.positions() == []
    assert reason in kills(d)


def test_liq_mcap_now_5pct(tmp_path):
    d, _ = make_desk(tmp_path, pairs=[pair(liq=80_000)])  # 8%: used to die at 25%, passes now
    d.scan_cycle()
    assert "liq_thin" not in kills(d)


def test_top10_excludes_pool_vault():
    from memedesk.sources.rugcheck import top10_pct
    assert top10_pct(report()) == pytest.approx(15.0)    # 22% pool vault is not a holder


def test_rpc_down_falls_back_to_rugcheck_authorities(tmp_path):
    rep = report(token={"supply": 10**9, "mintAuthority": "X1", "freezeAuthority": None})
    d, _ = make_desk(tmp_path, rpc=FakeRPC(fail=True), rep=rep)
    d.scan_cycle()
    assert "mint_authority_live" in kills(d)


def test_low_score_killed_and_random_still_draws_nothing(tmp_path):
    d, _ = make_desk(tmp_path, pairs=[pair(h1=-15, m5=-4, h6=-10, buys=200, sells=400)],
                     gecko=FakeGecko(n_buyers=5, smart=()))
    d.scan_cycle()
    assert "score_low" in kills(d, "scored")
    assert d.logger.positions() == []                     # 0 analyst passes => 0 random picks
    row = d.logger.db.execute("SELECT data FROM kills WHERE reason='score_low'").fetchone()
    assert "components" in row[0]


def test_random_draws_from_filter_survivors_matching_pass_count(tmp_path):
    mints = [f"Mint{i:02d}" + "1" * 36 for i in range(6)]
    pairs = [pair(mint=m, sym=f"T{i}") for i, m in enumerate(mints)]
    # only T0 is strong enough to score; the rest are weak but safe
    for p in pairs[1:]:
        p["priceChange"] = {"m5": -4, "h1": -15, "h6": -10}
        p["txns"]["h1"] = {"buys": 100, "sells": 400}
    d, _ = make_desk(tmp_path, pairs=pairs)
    d.analyst.gecko = type("G", (), {"trades": lambda self, pool, window_min=60:
                                     FakeGecko().trades(pool) if pool == f"pool_{mints[0]}" else []})()
    d.scan_cycle()
    scored = d.logger.positions(book="scored")
    rnd = d.logger.positions(book="random")
    assert [p.symbol for p in scored] == ["T0"]
    assert len(rnd) == 1 and rnd[0].mint in mints        # matched count, drawn from all 6 survivors


def test_random_cannot_skip_filter():
    from memedesk.agents.random_control import RandomControl
    c = make_candidate()
    with pytest.raises(HandoffError):
        RandomControl(1).pick([Vetted(candidate=c, facts=None, trail=["scout"])], 1)


def make_candidate():
    from memedesk.agents.scout import candidate_from_pair
    return candidate_from_pair(pair(), ["test"])


def test_killed_mint_is_on_cooldown(tmp_path):
    d, _ = make_desk(tmp_path, pairs=[pair(h1=95)])
    d.scan_cycle()
    d.scan_cycle()
    assert kills(d).count("pumped_1h") == 1


def test_executor_refuses_without_risk_approval(tmp_path):
    d, _ = make_desk(tmp_path)
    c, _ = d.scout.run()
    v = d.filter.run(c[0])
    sc = d.analyst.run(v)
    t = d.executor.propose(sc, 1000, 1000)
    with pytest.raises(HandoffError):
        d.executor.execute(t, RiskApproval(ticket_id="someone-else", stop_pct=15, max_size_usd=80))
    with pytest.raises(HandoffError):
        d.analyst.run(c[0])  # skipping the filter is refused


# ---------------------------------------------------------------- exits

def _pos(entry=1.0):
    return Position(id="x", book="scored", mint="m", symbol="M", pair_address="p", entry_price=entry,
                    initial_qty=100, qty=100, cost_usd=100, stop_price=entry * 0.85, peak_price=entry,
                    entry_liq_usd=100_000, score=80, opened_ts=time.time())


def test_hard_stop_minus_15():
    p, cfg = _pos(), Config()
    assert step(p, 0.90, 100_000, time.time(), cfg)[0] is None    # -10% survives now
    assert step(p, 0.85, 100_000, time.time(), cfg)[0] == ("exit", 1.0, "stop_loss")


def test_take_profit_then_runner_trail_floored_at_entry():
    p, cfg = _pos(), Config()
    assert step(p, 1.30, None, time.time(), cfg)[0] is None
    assert step(p, 1.36, None, time.time(), cfg)[0] == ("partial", 0.75, "take_profit")
    p.tp_taken = True
    p.stop_price = runner_stop(p, cfg)
    assert p.stop_price == pytest.approx(1.0)                     # 1.36*0.65 < entry => floor
    step(p, 2.0, None, time.time(), cfg)
    assert p.stop_price == pytest.approx(1.30)                    # 2.0 * 0.65
    assert step(p, 1.35, None, time.time(), cfg)[0] is None
    assert step(p, 1.29, None, time.time(), cfg)[0] == ("exit", 1.0, "runner_trail")


def test_runner_breakeven_exit():
    p, cfg = _pos(), Config()
    p.tp_taken, p.peak_price = True, 1.4
    p.stop_price = runner_stop(p, cfg)
    assert step(p, 0.99, None, time.time(), cfg)[0] == ("exit", 1.0, "runner_breakeven")


def test_max_hold_48h():
    p, cfg = _pos(), Config()
    p.opened_ts = time.time() - 49 * 3600
    assert step(p, 1.05, None, time.time(), cfg)[0] == ("exit", 1.0, "max_hold")


def test_liquidity_crash_exit():
    assert step(_pos(), 1.0, 40_000, time.time(), Config())[0][2] == "liq_crash"


def test_monitor_partial_then_close_books_pnl(tmp_path):
    d, dex = make_desk(tmp_path, random_control=False)
    d.scan_cycle()
    [p] = d.logger.positions("open")
    entry = p.entry_price
    dex.set_price(GOOD, entry * 1.40)
    d.risk.monitor(d.executor, threading.RLock())
    [p] = d.logger.positions("open")
    assert p.tp_taken and p.qty == pytest.approx(p.initial_qty * 0.25)
    assert p.stop_price == pytest.approx(entry)                  # floor at entry
    dex.set_price(GOOD, entry * 2.0)
    d.risk.monitor(d.executor, threading.RLock())
    dex.set_price(GOOD, entry * 1.25)                            # below 2.0*0.65 = 1.30
    d.risk.monitor(d.executor, threading.RLock())
    [c] = d.logger.positions("closed")
    assert c.exit_reason == "runner_trail" and c.pnl_usd > 0 and len(c.sells) == 2
    assert d.logger.on_cooldown(GOOD, "scored")                 # no instant re-entry
    st = d.logger.book_stats("scored")
    assert st["closed"] == 1 and st["tp_hits"] == 1
    assert d.broker.cash_usd("scored") == pytest.approx(920 + c.realized_usd)


def test_daily_drawdown_halts_entries(tmp_path):
    d, _ = make_desk(tmp_path, random_control=False)
    d.risk.halted("scored", 1000.0)
    d.logger.put("cash:scored", 750.0)                           # -25%
    d.scan_cycle()
    assert "halted" in kills(d, "scored") and not d.logger.positions()


# ---------------------------------------------------------------- reporting & infra

def test_compare_verdicts():
    from memedesk.agents.logger import Logger
    few = Logger.compare({"_returns": [5, 10]}, {"_returns": [1, 2]})
    assert "too early" in few["verdict"]
    win = Logger.compare({"_returns": [20 + i % 5 for i in range(30)]},
                         {"_returns": [-5 + i % 5 for i in range(30)]})
    assert win["t"] > 2 and "beating random" in win["verdict"]


def test_dashboard_state_and_server(tmp_path):
    import json
    import urllib.request
    from memedesk.dashboard import build_state, start_dashboard
    d, _ = make_desk(tmp_path, dashboard_port=0)
    d.scan_cycle()
    s = build_state(d)
    assert len(s["books"]["scored"]["positions"]) == 1 and len(s["books"]["random"]["positions"]) == 1
    assert s["books"]["scored"]["entries"] == 1 and s["books"]["scored"]["start_usd"] == 1000
    assert [x["side"] for x in s["decisions"]] == ["BUY", "BUY"]
    assert s["mode"] == "pipeline" and s["agents"]["filter"]["last"]
    assert any(t["symbol"] == "GOOD" for t in s["ticker"])
    srv = start_dashboard(d)
    port = srv.server_address[1]
    body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
    assert set(body["books"]) == {"scored", "random"}
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
    assert "MemeDesk" in page and "esc(" in page
    srv.shutdown()


def test_http_backoff_and_cache(monkeypatch):
    import io
    import urllib.error
    from memedesk.http import HttpClient
    calls = {"n": 0}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, None)
        return Resp(b'{"ok": 1}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    h = HttpClient(rpm={"x.test": 6000})
    assert h.get("https://x.test/a", cache_ttl=60) == {"ok": 1}
    assert h.get("https://x.test/a", cache_ttl=60) == {"ok": 1}
    assert calls["n"] == 2 and h.stats["rate_limited"] == 1 and h.stats["cache_hits"] == 1


def test_scoring_bounds():
    for args in [(-50, -90, -90), (0, 0, 0), (5, 25, 50), (50, 79, 500)]:
        assert 0 <= score_momentum(*args) <= 100
    assert score_unique_buyers(None) == 0 and score_unique_buyers(10_000) == 100
    assert lin(5, 0, 10) == 50


def test_paper_only(monkeypatch, tmp_path):
    monkeypatch.setenv("DESK_MODE", "live")
    with pytest.raises(ValueError):
        Config.load(tmp_path / "none.env")


def test_no_compiled_or_third_party_imports():
    import ast
    import pathlib
    import sys
    allowed = set(sys.stdlib_module_names) | {"memedesk"}
    for f in pathlib.Path("memedesk").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for n in names:
                assert n.split(".")[0] in allowed, f"{f}: imports {n}"


def test_active_profile_lets_soft_unknowns_through_but_not_hard_ones(tmp_path, monkeypatch):
    monkeypatch.delenv("DESK_PROFILE", raising=False)
    monkeypatch.delenv("DESK_MIN_SCORE", raising=False)
    c = Config.load(tmp_path / "none.env")
    assert c.profile == "active" and c.min_score == 60
    monkeypatch.setenv("DESK_MIN_SCORE", "75")
    assert Config.load(tmp_path / "none.env").min_score == 75          # explicit setting wins
    d, _ = make_desk(tmp_path, bundles=FakeBundles(None), profile="active")
    d.scan_cycle()
    assert "unknown_bundle" not in kills(d) and d.logger.positions("open", "scored")
    d2, _ = make_desk(tmp_path / "b", rpc=FakeRPC(fail=True), rep=report(token={"supply": 1}), profile="active")
    d2.scan_cycle()
    assert "unknown_mint_authority" in kills(d2) and not d2.logger.positions()


def test_organic_score_filter(tmp_path):
    d, _ = make_desk(tmp_path, jup=FakeJup(score=12, label="low"))
    d.scan_cycle()
    assert "organic_low" in kills(d) and not d.logger.positions()
    d2, _ = make_desk(tmp_path / "b", jup=FakeJup(score=None))       # unknown: not a kill by default
    d2.scan_cycle()
    assert d2.logger.positions("open", "scored")


def test_coingecko_backup_only_on_429_and_capped(tmp_path):
    from memedesk.agents.logger import Logger
    from memedesk.http import DataError
    from memedesk.sources.geckoterminal import GeckoTerminal, MonthlyBudget

    class H:
        def __init__(self):
            self.urls = []

        def get(self, url, **kw):
            self.urls.append(url)
            if "geckoterminal" in url:
                raise DataError("GET api.geckoterminal.com failed after 2 tries: HTTP 429")
            return {"data": []}

    lg = Logger(tmp_path)
    h = H()
    g = GeckoTerminal(h, "https://api.geckoterminal.com/api/v2", "demo-key", MonthlyBudget(lg, "cg", 2))
    g.trades("pool1")
    g.trades("pool2")
    assert sum("coingecko.com/api/v3/onchain" in u for u in h.urls) == 2
    with pytest.raises(DataError, match="budget used up"):
        g.trades("pool3")
    h2 = H()
    with pytest.raises(DataError, match="429"):                      # no key: no backup, just the error
        GeckoTerminal(h2, "https://api.geckoterminal.com/api/v2").trades("pool4")
    assert all("coingecko.com" not in u for u in h2.urls)


def test_pwa_manifest_and_icons(tmp_path):
    import json
    import urllib.request
    from memedesk.dashboard import start_dashboard
    d, _ = make_desk(tmp_path, dashboard_port=0)
    srv = start_dashboard(d)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    m = json.loads(urllib.request.urlopen(base + "/manifest.webmanifest").read())
    assert m["display"] == "standalone" and m["icons"][0]["src"] == "/icon-192.png"
    png = urllib.request.urlopen(base + "/icon-192.png").read()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 500
    assert 'rel="manifest"' in urllib.request.urlopen(base + "/").read().decode()
    srv.shutdown()


def test_missing_trade_tape_defers_instead_of_scoring_zero(tmp_path):
    from memedesk.http import DataError

    class G429(FakeGecko):
        def trades(self, pool, window_min=60):
            raise DataError("GET api.geckoterminal.com failed after 4 tries: HTTP 429")

    d, _ = make_desk(tmp_path, gecko=G429())
    d.scan_cycle()
    rows = d.logger.db.execute("SELECT reason FROM kills WHERE agent='analyst'").fetchall()
    assert [r[0] for r in rows] == ["no_trade_data"]
    assert d.logger.on_cooldown(GOOD, "scored") and not d.logger.on_cooldown(GOOD)


def test_fast_profile_buys_on_first_sighting(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_PROFILE", "fast")
    c = Config.load(tmp_path / "none.env")
    assert (c.min_score, c.scan_interval_s, c.holder_growth_optional) == (55, 60, True)
    d, _ = make_desk(tmp_path, profile="fast", min_score=55, holder_growth_optional=True)
    d.logger.db.execute("DELETE FROM holders")            # first sighting: no growth history at all
    d.logger.db.commit()
    d.scan_cycle()
    assert d.logger.positions("open", "scored")
