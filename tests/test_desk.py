import copy
import threading
import time

import pytest

from memedesk.agents.risk import step
from memedesk.agents.analyst import score_momentum, score_unique_buyers, lin
from memedesk.config import Config
from memedesk.desk import Desk
from memedesk.http import DataError
from memedesk.models import HandoffError, Kill, Position, RiskApproval

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
        "markets": [{"pubkey": f"pool_{GOOD}", "lp": {"lpLockedPct": 100, "quoteUSD": 150000}}],
        "topHolders": [{"owner": "SmartA", "pct": 1.2, "insider": False}],
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
                    "liq": self.pairs[m]["liquidity"]["usd"], "pair": self.pairs[m]}
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


class NoGrok:
    enabled = False

    def morning_report(self, s):
        return ""


def make_desk(tmp_path, pairs=None, rep=None, rpc=None, bundles=None, gecko=None, **cfg_over):
    cfg = Config(data_dir=str(tmp_path), **cfg_over)
    cfg.validate()
    dex = FakeDex(pairs or [pair()])
    d = Desk(cfg, dex=dex, gecko=gecko or FakeGecko(), rpc=rpc or FakeRPC(),
             rug=FakeRug(report() if rep is None else rep), bundles=bundles or FakeBundles(),
             grok=NoGrok(), smart_wallets={"SmartA", "SmartB"})
    # pre-seed a holder snapshot 30 min ago so holder growth is measurable
    d.logger.db.execute("INSERT INTO holders(mint,ts,holders) VALUES(?,?,?)", (GOOD, time.time() - 1800, 2000))
    d.logger.db.commit()
    return d, dex


def kills(d):
    return [r[0] for r in d.logger.db.execute("SELECT reason FROM kills")]


def test_good_token_enters_with_stop_and_8pct_cap(tmp_path):
    d, _ = make_desk(tmp_path)
    out = d.scan_cycle()
    assert out.get("entered") == 1, kills(d)
    [p] = d.logger.positions("open")
    assert p.cost_usd == pytest.approx(80.0)            # 8% of $1000
    assert p.stop_price == pytest.approx(p.entry_price * 0.94)
    assert d.broker.cash_usd() == pytest.approx(920.0)
    trail = [r[0] for r in d.logger.db.execute("SELECT reason FROM events WHERE kind='handoff'")]
    assert trail == ["scout->filter", "filter->analyst", "analyst->executor",
                     "executor->risk", "risk->executor"]


def test_config_cannot_raise_position_above_8pct(tmp_path):
    d, _ = make_desk(tmp_path, position_pct=25)
    d.scan_cycle()
    [p] = d.logger.positions("open")
    assert p.cost_usd <= 80.0 + 1e-6


@pytest.mark.parametrize("kw,reason", [
    (dict(pairs=[pair(age_h=0.2)]), "too_young"),
    (dict(pairs=[pair(age_h=80)]), "too_old"),
    (dict(pairs=[pair(mcap=200_000, liq=60_000)]), "mcap_low"),
    (dict(pairs=[pair(mcap=25_000_000, liq=8_000_000)]), "mcap_high"),
    (dict(pairs=[pair(h1=95)]), "pumped_1h"),
    (dict(pairs=[pair(liq=100_000)]), "liq_thin"),
    (dict(rpc=FakeRPC(mint_auth="Auth111111")), "mint_authority_live"),
    (dict(rpc=FakeRPC(freeze_auth="Frz1111111")), "freeze_authority_live"),
    (dict(rep=report(markets=[{"pubkey": f"pool_{GOOD}", "lp": {"lpLockedPct": 40}}])), "lp_unlocked"),
    (dict(rep=report(creatorBalance=50_000_000)), "dev_heavy"),
    (dict(rep=report(risks=[{"name": "Creator history of rugged tokens"}])), "dev_past_rugs"),
    (dict(bundles=FakeBundles(14.0)), "bundled_launch"),
    (dict(bundles=FakeBundles(None)), "unknown_bundle"),
    (dict(rep=report(rugged=True)), "rugged"),
])
def test_filter_kills(tmp_path, kw, reason):
    d, _ = make_desk(tmp_path, **kw)
    d.scan_cycle()
    assert d.logger.positions() == []
    assert reason in kills(d)


def test_rpc_down_falls_back_to_rugcheck_authorities(tmp_path):
    rep = report(token={"supply": 10**9, "mintAuthority": "X1", "freezeAuthority": None})
    d, _ = make_desk(tmp_path, rpc=FakeRPC(fail=True), rep=rep)
    d.scan_cycle()
    assert "mint_authority_live" in kills(d)


def test_low_score_killed_and_logged(tmp_path):
    d, _ = make_desk(tmp_path, pairs=[pair(h1=-15, m5=-4, h6=-10, buys=200, sells=400)],
                     gecko=FakeGecko(n_buyers=5, smart=()))
    d.scan_cycle()
    assert "score_low" in kills(d)
    row = d.logger.db.execute("SELECT data FROM kills WHERE reason='score_low'").fetchone()
    assert "components" in row[0]


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
        d.executor.execute(t, RiskApproval(ticket_id="someone-else", stop_pct=6,
                                           trail_activate_pct=12, trail_pct=6, max_size_usd=80))
    with pytest.raises(HandoffError):
        d.analyst.run(c[0])  # skipping the filter is refused


def _pos(entry=1.0):
    return Position(id="x", mint="m", symbol="M", pair_address="p", mode="paper", entry_price=entry,
                    qty=80, cost_usd=80, stop_price=entry * 0.94, stop_pct=6, trail_activate_pct=12,
                    trail_pct=6, peak_price=entry, entry_liq_usd=100_000, score=80, opened_ts=time.time())


def test_risk_hard_stop():
    p, cfg = _pos(), Config()
    assert step(p, 0.97, 100_000, time.time(), cfg)[0] is None
    assert step(p, 0.94, 100_000, time.time(), cfg)[0] == "stop_loss"


def test_risk_trailing():
    p, cfg = _pos(), Config()
    step(p, 1.10, None, time.time(), cfg)
    assert not p.trailing and p.stop_price == pytest.approx(0.94)
    step(p, 1.12, None, time.time(), cfg)
    assert p.trailing and p.stop_price == pytest.approx(1.12 * 0.94)
    step(p, 1.50, None, time.time(), cfg)
    assert p.stop_price == pytest.approx(1.41)
    assert step(p, 1.45, None, time.time(), cfg)[0] is None
    assert step(p, 1.40, None, time.time(), cfg)[0] == "trail_stop"


def test_risk_liquidity_crash_exit():
    p = _pos()
    assert step(p, 1.0, 40_000, time.time(), Config())[0] == "liq_crash"


def test_monitor_closes_and_books_pnl(tmp_path):
    d, dex = make_desk(tmp_path)
    d.scan_cycle()
    [p] = d.logger.positions("open")
    dex.set_price(GOOD, 0.001 * 1.30)
    d.risk.monitor(d.executor, threading.RLock())
    assert d.logger.positions("open")[0].trailing
    dex.set_price(GOOD, 0.001 * 1.15)
    d.risk.monitor(d.executor, threading.RLock())
    [c] = d.logger.positions("closed")
    assert c.exit_reason == "trail_stop" and c.pnl_usd > 0
    assert d.logger.on_cooldown(GOOD)                  # no instant re-entry
    assert d.logger.summary()["closed"] == 1


def test_daily_drawdown_halts_entries(tmp_path):
    d, _ = make_desk(tmp_path)
    d.risk.halted(1000.0)                               # day start equity
    d.logger.put("paper_cash", 800.0)                   # -20%
    d.scan_cycle()
    assert "halted" in kills(d) and not d.logger.positions()


def test_scoring_bounds():
    for args in [(-50, -90, -90), (0, 0, 0), (5, 25, 50), (50, 79, 500)]:
        assert 0 <= score_momentum(*args) <= 100
    assert score_unique_buyers(None) == 0 and score_unique_buyers(10_000) == 100
    assert lin(5, 0, 10) == 50


def test_live_requires_explicit_confirmation():
    with pytest.raises(ValueError):
        Config(mode="live").validate()
