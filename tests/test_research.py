import threading
import time
from dataclasses import replace

import pytest

from memedesk.config import Config
from memedesk.evaluate import bootstrap_ci, holm, permutation_p, run as evaluate
from memedesk.research import VARIANTS, rule_version
from memedesk.sources.pumpportal import PumpPortalFeed
from memedesk.sources.solana_rpc import token2022_risks
from tests.test_desk import FakeLLM, FakeRPC, _ai_desk, _mom_desk, kills, make_desk, mom_pair


# ---------------------------------------------------------------- rule versions

def test_rule_version_changes_with_rules_not_with_keys_or_paths():
    base = Config()
    assert rule_version(base) == rule_version(replace(base, gemini_api_key="x", data_dir="/elsewhere"))
    assert rule_version(base) != rule_version(replace(base, mom_stop_pct=base.mom_stop_pct + 1))
    assert len(rule_version(base)) == 8


def test_entries_and_banner_carry_the_rule_version(tmp_path):
    d, _ = _mom_desk(tmp_path, [mom_pair()])
    d.scan_cycle()
    [p] = d.logger.positions("open", "momentum")
    assert p.rule_version == d.version
    data = d.logger.db.execute("SELECT data FROM events WHERE kind='decision'").fetchone()[0]
    assert f'"version":"{d.version}"' in data


# ---------------------------------------------------------------- candidates + forward returns

def test_candidates_are_logged_with_decisions_and_forward_returns_fill_in(tmp_path):
    d, dex = _mom_desk(tmp_path, [mom_pair(0), mom_pair(1, m5=90)])     # one bought, one "too_late"
    d.research.rng.random = lambda: 0.0                                 # keep every sampled reject
    d.scan_cycle()
    rows = d.logger.db.execute("SELECT book, decision, reason, mint FROM candidates").fetchall()
    assert ("momentum", "accept", "", mom_pair(0)["baseToken"]["address"]) in rows
    assert any(r[0] == "momentum" and r[1] == "reject" and r[2] == "too_late" for r in rows)
    # pretend 20 minutes have passed and the bought coin is up 10%
    d.logger.db.execute("UPDATE candidates SET ts = ts - 1200")
    m0 = mom_pair(0)["baseToken"]["address"]
    p0 = d.logger.db.execute("SELECT price FROM candidates WHERE mint=? AND decision='accept'", (m0,)).fetchone()[0]
    dex.set_price(m0, p0 * 1.10)
    assert d.research_tick() > 0
    r5, r15, r1h = d.logger.db.execute("SELECT ret_5m, ret_15m, ret_1h FROM candidates WHERE mint=? "
                                       "AND decision='accept'", (m0,)).fetchone()
    assert r5 == pytest.approx(10.0, abs=0.01) and r15 == pytest.approx(10.0, abs=0.01) and r1h is None


def test_vanished_coin_counts_as_total_loss(tmp_path):
    d, dex = _mom_desk(tmp_path, [mom_pair()])
    d.scan_cycle()
    d.logger.db.execute("UPDATE candidates SET ts = ts - 400")
    dex.pairs.clear()
    d.research_tick()
    assert {r[0] for r in d.logger.db.execute("SELECT ret_5m FROM candidates")} == {-100.0}


def test_filter_rejects_are_logged_too(tmp_path):
    d, _ = make_desk(tmp_path, rpc=FakeRPC(mint_auth="Live1"))
    d.scan_cycle()
    assert d.logger.db.execute("SELECT COUNT(*) FROM candidates WHERE stage='filter' AND decision='reject' "
                               "AND reason='mint_authority_live'").fetchone()[0] == 1


# ---------------------------------------------------------------- price path + shadow exits

def test_price_path_and_shadow_exits_follow_the_position(tmp_path):
    d, dex = _mom_desk(tmp_path, [mom_pair()])
    d.scan_cycle()
    [p] = d.logger.positions("open", "momentum")
    assert len(d.research.shadow_open) == len(VARIANTS)
    lock = threading.RLock()
    dex.set_price(p.mint, p.entry_price * 1.30)                         # +30%: tp25 variant takes all
    d.risk.monitor(d.executor, lock)
    path = d.logger.db.execute("SELECT price FROM pricepath WHERE pos_id=?", (p.id,)).fetchall()
    assert len(path) == 1 and path[0][0] == pytest.approx(p.entry_price * 1.30)
    done = dict(d.logger.db.execute("SELECT variant, reason FROM shadow WHERE closed_ts IS NOT NULL").fetchall())
    assert done == {"stop12_tp25_all": "take_all"}
    dex.set_price(p.mint, p.entry_price * 1.02)                         # 22% off the peak: trail20 sells
    d.risk.monitor(d.executor, lock)
    dex.set_price(p.mint, p.entry_price * 0.70)                         # -30%: every stop fires
    d.risk.monitor(d.executor, lock)
    d.risk.monitor(d.executor, lock)
    reasons = dict(d.logger.db.execute("SELECT variant, reason FROM shadow").fetchall())
    assert reasons["stop12_hold4h"] == "stop" and reasons["stop12_trail20"] == "trail"
    assert not d.research.shadow_open                                    # all closed, even after the real exit
    pnl = d.logger.db.execute("SELECT pnl_pct FROM shadow WHERE variant='stop12_hold4h'").fetchone()[0]
    assert pnl == pytest.approx(100 * (0.70 * 0.99 - 1), abs=0.01)


def test_open_shadows_survive_a_restart(tmp_path):
    d, _ = _mom_desk(tmp_path, [mom_pair()])
    d.scan_cycle()
    from memedesk.research import Research
    r2 = Research(d.logger, d.cfg, d.version)
    assert len(r2.shadow_open) == len(VARIANTS)


# ---------------------------------------------------------------- AI book

def test_ai_take_profit_and_time_limit_from_its_plan(tmp_path):
    plan = {"stop_pct": 10, "tp_pct": 20, "max_hold_min": 30}
    llm = FakeLLM(lambda s: {"buys": [{"mint": c["mint"], **plan, "reason": "x"} for c in s["candidates"][:2]],
                             "sells": []})
    d, dex = _ai_desk(tmp_path, [mom_pair(0), mom_pair(1)], llm)
    d.scan_cycle()
    d.ai_tick()
    a, b = sorted(d.logger.positions("open", "ai"), key=lambda p: p.mint)
    assert a.stop_price == pytest.approx(a.entry_price * 0.90)
    dex.set_price(a.mint, a.entry_price * 1.25)
    b.deadline_ts = time.time() - 1
    d.logger.save_position(b)
    d.risk.monitor(d.executor, threading.RLock())
    reasons = {p.mint: p.exit_reason for p in d.logger.positions("closed", "ai")}
    assert reasons == {a.mint: "ai_take_profit", b.mint: "ai_time_up"}


def test_ai_sees_its_recent_trades_and_backs_off_on_429(tmp_path):
    from memedesk.http import DataError
    calls = {"n": 0}

    def reply(s):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"buys": [{"mint": s["candidates"][0]["mint"], "reason": "go"}], "sells": []}
        if calls["n"] == 2:
            return {"buys": [], "sells": [{"id": s["open_positions"][0]["id"], "reason": "done"}]}
        raise DataError("HTTP 429 from generativelanguage.googleapis.com: RESOURCE_EXHAUSTED")

    llm = FakeLLM(reply)
    d, _ = _ai_desk(tmp_path, [mom_pair()], llm)
    d.scan_cycle()
    d.ai_tick()
    d.ai_tick()
    d.logger.db.execute("DELETE FROM cooldowns")                       # let it see the coin again
    assert d.ai_tick() == {"ai": "rate limited"}
    hist = llm.seen[2]["recent_closed_trades"]
    assert len(hist) == 1 and hist[0]["exit"] == "ai_sell"
    assert d.ai_tick() == {"ai": "waiting after rate limit"} and calls["n"] == 3     # no call while paused
    assert d.ai.backoff_until - time.time() == pytest.approx(300, abs=5)


def test_ai_interval_is_five_minutes():
    assert Config().ai_interval_s == 300


# ---------------------------------------------------------------- Token-2022

def test_token2022_dangerous_extensions_are_found():
    info = {"extensions": [
        {"extension": "tokenMetadata", "state": {"name": "x"}},
        {"extension": "permanentDelegate", "state": {"delegate": "Evil111"}},
        {"extension": "transferHook", "state": {"programId": "Hook111"}},
        {"extension": "transferFeeConfig", "state": {"olderTransferFee": {"transferFeeBasisPoints": 0},
                                                     "newerTransferFee": {"transferFeeBasisPoints": 500}}},
        {"extension": "defaultAccountState", "state": {"accountState": "frozen"}},
        {"extension": "pausableConfig", "state": {"authority": "A"}},
        {"extension": "nonTransferable"}]}
    assert token2022_risks(info) == ["permanent_delegate", "transfer_hook", "transfer_fee_5%",
                                     "default_frozen", "pausable", "non_transferable"]
    harmless = {"extensions": [{"extension": "metadataPointer", "state": {}},
                               {"extension": "transferHook", "state": {"programId": None}},
                               {"extension": "transferFeeConfig", "state": {}}]}
    assert token2022_risks(harmless) == [] and token2022_risks({}) == []


def test_token2022_risk_blocks_code_books_but_the_ai_is_told(tmp_path):
    d, _ = make_desk(tmp_path, rpc=FakeRPC(t22=["permanent_delegate"]))
    d.scan_cycle()
    assert "token2022_risk" in kills(d) and not d.logger.positions("open")
    llm = FakeLLM({"buys": [], "sells": []})
    d2, _ = _ai_desk(tmp_path / "ai", [mom_pair()], llm, rpc=FakeRPC(t22=["transfer_hook"]))
    d2.scan_cycle()
    d2.ai_tick()
    assert llm.seen[0]["candidates"][0]["safety"]["token2022_dangerous_extensions"] == ["transfer_hook"]


# ---------------------------------------------------------------- insider sells (PumpPortal trades)

class FakeWS:
    def __init__(self):
        self.sent = []

    def send_text(self, t):
        self.sent.append(t)


def test_feed_subscribes_held_coins_and_flags_creator_and_whale_sells():
    f = PumpPortalFeed()
    ws = f.ws = FakeWS()
    mint = "A" * 40 + "pump"
    f.watch({mint: "Creator1"})
    assert '"subscribeTokenTrade"' in ws.sent[0] and mint in ws.sent[0]
    f.handle({"mint": mint, "txType": "buy", "traderPublicKey": "Creator1", "tokenAmount": 5e7})
    f.handle({"mint": mint, "txType": "sell", "traderPublicKey": "Small", "tokenAmount": 1e6})
    assert f.alert_for(mint) is None and not f.queue                    # trades never become "new token" seeds
    f.handle({"mint": mint, "txType": "sell", "traderPublicKey": "Creator1", "tokenAmount": 1e6})
    assert "creator" in f.alert_for(mint)
    f.handle({"mint": mint, "txType": "sell", "traderPublicKey": "Whale", "tokenAmount": 2e7})   # 2% of supply
    assert "2.0% of supply" in f.alert_for(mint)
    f.watch({})
    assert '"unsubscribeTokenTrade"' in ws.sent[-1]


def test_insider_sell_exits_the_position(tmp_path):
    d, _ = _mom_desk(tmp_path, [mom_pair()])
    feed = PumpPortalFeed()
    d.risk.feed = feed
    d.scan_cycle()
    [p] = d.logger.positions("open", "momentum")
    d.risk.monitor(d.executor, threading.RLock())
    assert p.mint in feed.watched
    feed.alerts[p.mint] = (time.time(), "creator sold")
    d.risk.monitor(d.executor, threading.RLock())
    assert d.logger.positions("closed", "momentum")[0].exit_reason == "insider_sell"


# ---------------------------------------------------------------- evaluate

def test_statistics_helpers():
    lo, hi = bootstrap_ci([1.0] * 10 + [3.0] * 10)
    assert 1.0 < lo < 2.0 < hi < 3.0
    assert permutation_p([10.0] * 30, [-10.0] * 30) < 0.01
    assert permutation_p([1.0, -1.0] * 15, [1.0, -1.0] * 15) > 0.5
    adj = holm({"a": 0.01, "b": 0.04, "c": float("nan")})
    assert adj["a"] == pytest.approx(0.02) and adj["b"] == pytest.approx(0.04) and adj["c"] != adj["c"]


def test_evaluate_runs_on_a_real_desk(tmp_path):
    d, dex = _mom_desk(tmp_path, [mom_pair()])
    d.scan_cycle()
    [p] = d.logger.positions("open", "momentum")
    dex.set_price(p.mint, p.entry_price * 0.70)
    d.risk.monitor(d.executor, threading.RLock())
    lines = []
    res = evaluate(d.logger, d.cfg.books, out=lines.append)
    text = "\n".join(lines)
    assert res["stats"]["momentum"]["n"] == 1 and "too few to judge" in text
    assert "shadow exits (same entries" in text and "stop12_hold4h" in text
    assert "candidates · mean forward return" in text


# ---------------------------------------------------------------- manual sell button

def test_manual_sell_from_the_dashboard(tmp_path):
    import json
    import urllib.error
    import urllib.request
    from memedesk.dashboard import build_state, start_dashboard
    d, dex = _mom_desk(tmp_path, [mom_pair()], dashboard_port=0)
    d.scan_cycle()
    [p] = d.logger.positions("open", "momentum")
    dex.set_price(p.mint, p.entry_price * 1.10)
    srv = start_dashboard(d)
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/sell"
    token = build_state(d)["token"]
    assert build_state(d)["books"]["momentum"]["positions"][0]["id"] == p.id

    def post(body, headers=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            r = urllib.request.urlopen(req)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    assert post({"id": p.id, "token": "wrong"}, {"X-MemeDesk": "1"})[0] == 403        # other sites can't sell
    assert post({"id": p.id, "token": token})[0] == 403                               # header required
    assert post({"id": p.id, "token": token}, {"X-MemeDesk": "1", "Host": "evil.example"})[0] == 403
    code, res = post({"id": p.id, "token": token}, {"X-MemeDesk": "1"})
    assert code == 200 and res["ok"] and "+" in res["msg"]
    [c] = d.logger.positions("closed", "momentum")
    assert c.exit_reason == "manual_sell" and c.pnl_usd > 0
    assert post({"id": p.id, "token": token}, {"X-MemeDesk": "1"})[0] == 409          # already sold
    reason = d.logger.db.execute("SELECT detail FROM events WHERE kind='decision' AND reason='SELL'").fetchone()[0]
    assert reason.startswith("sold by you")
    srv.shutdown()
