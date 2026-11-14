import time

import pytest

from memedesk.agents.whale_scout import bot_reason, max_multiple, realized_pnl_sol, wallet_score
from memedesk.sources.chain import ChainReader, Swap, parse_helius_tx, parse_rpc_tx
from tests.test_desk import FakeJup, FakeGecko, NoGrok, FakeRPC, FakeRug, FakeBundles, FakeDex, pair, report
from memedesk.config import Config
from memedesk.desk import Desk

NOW = time.time()
W = {f"W{i}": f"Win{i}" + "x" * 37 for i in range(1, 5)}   # winner mints (not pump mints)
X = "XmintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"


def candles(mult):
    t0 = NOW - 6 * 86400
    return [[t0 + i * 14400, 1, 1 + (mult - 1) * i / 30, 1 if i else 1, 1, 100] for i in range(31)]


class WhaleGecko(FakeGecko):
    def discover_pools(self):
        return [{"pool": f"pool_{m}", "mint": m, "symbol": k} for k, m in W.items()]

    def ohlcv(self, pool, **kw):
        return candles({"W4": 2}.get(next(k for k, m in W.items() if pool.endswith(m)), 6))


def sw(sig, wallet, mint, d_tok, d_sol, ts=NOW - 3600, slot=100):
    return Swap(sig, slot, ts, wallet, mint, d_tok, d_sol)


class FakeChain:
    backend = "fake"
    rpc_url = "fake"

    def __init__(self):
        early = {"W1": ["A", "B", "C", "D", "E", "F"], "W2": ["A", "B", "C", "D", "F"], "W3": ["A"], "W4": ["A", "B"]}
        self.by_addr = {}
        n = 0
        for k, wallets in early.items():
            rows = []
            for w in wallets:
                n += 1
                ts = NOW - 10 * 86400 if w == "F" else NOW - 3 * 86400
                rows.append(sw(f"s{n}", w, W[k], 1000, -1.0, ts=ts, slot=n))
            self.by_addr[f"pool_{W[k]}"] = rows
        # A sold half its W1 bag for 4 SOL -> realized +3.5 SOL
        self.extra = [sw("sa", "A", W["W1"], -500, 4.0, slot=999)]
        # D is a bot: 60 trades in one hour
        self.extra += [sw(f"d{i}", "D", "Junk", 10, -0.01, ts=NOW - 7200 + i * 30, slot=2000 + i) for i in range(60)]
        self.recent = {}

    def oldest_signatures(self, addr, n, pages):
        return [{"signature": s.sig} for s in self.by_addr.get(addr, [])]

    def swaps_for(self, sigs):
        rows = [s for v in self.by_addr.values() for s in v if s.sig in sigs]
        self.store.save_swaps(rows)        # the real ChainReader caches every parsed swap
        return rows

    def wallet_recent_swaps(self, wallet, limit=15):
        rows = self.recent.get(wallet, [])
        self.store.save_swaps(rows)
        return rows


def whale_desk(tmp_path):
    cfg = Config(data_dir=str(tmp_path), random_seed=3, smart_wallets_file=str(tmp_path / "smart_wallets.txt"))
    cfg.validate()
    chain = FakeChain()
    rep = report()
    rep["creator"] = "C"
    d = Desk(cfg, dex=FakeDex([pair(mint=X, sym="XWHALE")]), gecko=WhaleGecko(), rpc=FakeRPC(),
             rug=FakeRug(rep), bundles=FakeBundles(), grok=NoGrok(), smart_wallets={"SmartA"}, chain=chain, jup=FakeJup())
    chain.store = d.logger
    d.logger.save_swaps(chain.extra)
    d.logger.db.execute("INSERT INTO holders(mint,ts,holders) VALUES(?,?,?)", (X, time.time() - 1800, 2000))
    d.logger.db.commit()
    return d, chain


def test_max_multiple_and_scores():
    assert max_multiple(candles(6), NOW - 7 * 86400) == pytest.approx(6, rel=0.01)
    assert max_multiple([[NOW, 1, 2, 1, 1, 0], [NOW + 1, 1, 9, 3, 1, 0]], 0) == 9   # 9 / earlier low 1
    assert wallet_score(5, 20) == 100 and wallet_score(2, None) == 32 and wallet_score(5, -1) == 30


def test_realized_pnl_and_bots():
    rows = [sw("1", "a", "m", 100, -1), sw("2", "a", "m", -50, 2)]
    assert realized_pnl_sol(rows) == pytest.approx(1.5)
    assert realized_pnl_sol([sw("1", "a", "m", 100, -1)]) is None
    assert "bot" in bot_reason([sw(str(i), "a", "m", 1, -1, ts=NOW + i) for i in range(51)], 50)
    assert "mev" in bot_reason([sw("1", "a", "m", 1, -1, slot=5), sw("2", "a", "m", -1, 1, slot=5)], 50)
    assert bot_reason([sw("1", "a", "m", 1, -1)], 50) is None


def test_discovery_builds_wallet_book_with_reasons(tmp_path):
    d, _ = whale_desk(tmp_path)
    out = d.whale.run_discovery()
    assert out["new_winners"] == 3                       # W4 only did 2x
    tracked = {w["address"]: w for w in d.logger.tracked_wallets()}
    assert set(tracked) == {"A", "B"}                    # C dev, D bot, E one winner, F inactive
    assert tracked["A"]["score"] > tracked["B"]["score"] and tracked["A"]["pnl_sol"] == pytest.approx(3.5)
    text = (tmp_path / "smart_wallets.txt").read_text()
    assert text.splitlines()[2].startswith("A  # score=")
    adds = [r[0] for r in d.logger.db.execute("SELECT detail FROM events WHERE kind='wallet_add'")]
    assert len(adds) == 2 and any("early in 3 winners" in a for a in adds)
    # B goes quiet for 8 days -> dropped with reason
    d.logger.db.execute("UPDATE swaps SET ts=? WHERE wallet='B'", (NOW - 8 * 86400,))
    d.logger.db.commit()
    d.whale.rebuild_wallets()
    drops = [r[0] for r in d.logger.db.execute("SELECT detail FROM events WHERE kind='wallet_drop'")]
    assert drops == ["B…: inactive 7d"]
    assert "B  #" not in (tmp_path / "smart_wallets.txt").read_text()
    # the analyst picks the file up without a restart
    d.analyst._fixed = None
    assert d.analyst.smart == {"A"}


def test_whale_mode_signal_goes_through_filter_analyst_risk(tmp_path):
    d, chain = whale_desk(tmp_path)
    d.whale.run_discovery()
    assert d.mode == "whale mode"
    chain.recent = {"A": [sw("x1", "A", X, 500, -2, ts=time.time() - 120, slot=777)],
                    "B": [sw("x2", "B", X, 800, -3, ts=time.time() - 60, slot=777)]}
    d.whale_tick()
    c = d.whale_queue.get_nowait()
    assert c.trail == ["whale_scout"] and c.reason.startswith("2 whales same block")
    assert d.process_whale(c) == "entered"
    [p] = d.logger.positions("open", "scored")
    assert p.symbol == "XWHALE"
    why = d.logger.db.execute("SELECT detail FROM events WHERE kind='decision' AND reason='BUY'").fetchone()[0]
    assert why.startswith("2 whales same block") and "score" in why
    d.whale_tick()                                       # same signal is not repeated within the hour
    assert d.whale_queue.empty()


def test_whale_candidate_still_killed_by_filter(tmp_path):
    d, chain = whale_desk(tmp_path)
    d.whale.run_discovery()
    d.filter.rug = FakeRug(report(rugged=True))
    chain.recent = {w: [sw(f"y{w}", w, X, 5, -1, ts=time.time() - 30, slot=9)] for w in ("A", "B")}
    d.whale_tick()
    assert d.process_whale(d.whale_queue.get_nowait()) == "filter"
    assert not d.logger.positions()


def test_parse_rpc_and_helius_transactions():
    rpc_tx = {"slot": 5, "blockTime": 1700000000, "meta": {
        "err": None, "preBalances": [5_000_000_000], "postBalances": [3_990_000_000],
        "preTokenBalances": [], "postTokenBalances": [
            {"mint": "MEME", "owner": "Trader", "uiTokenAmount": {"uiAmount": 1234.5}},
            {"mint": "MEME", "owner": "PoolOwner", "uiTokenAmount": {"uiAmount": 1}}]},
        "transaction": {"message": {"accountKeys": [{"pubkey": "Trader"}, {"pubkey": "x"}]}}}
    [s] = parse_rpc_tx("sig", rpc_tx)
    assert (s.wallet, s.mint, s.d_token, s.side) == ("Trader", "MEME", 1234.5, "buy")
    assert s.d_sol == pytest.approx(-1.01)
    assert parse_rpc_tx("sig", {**rpc_tx, "meta": {**rpc_tx["meta"], "err": {"x": 1}}}) == []
    hel = {"signature": "h", "slot": 9, "timestamp": 1700000001, "feePayer": "T",
           "tokenTransfers": [{"mint": "MEME", "tokenAmount": 50, "fromUserAccount": "T", "toUserAccount": "P"},
                              {"mint": "So11111111111111111111111111111111111111112", "tokenAmount": 2,
                               "fromUserAccount": "P", "toUserAccount": "T"}],
           "accountData": [{"account": "T", "nativeBalanceChange": -5000}]}
    [h] = parse_helius_tx(hel)
    assert h.side == "sell" and h.d_token == -50 and h.d_sol == pytest.approx(2 - 5e-6)


def test_oldest_signatures_paging(tmp_path):
    class R(ChainReader):
        def __init__(self, total):
            self.total, self.calls = total, 0

        def _rpc(self, method, params):
            self.calls += 1
            before = params[1].get("before")
            start = int(before[1:]) - 1 if before else self.total - 1
            return [{"signature": f"s{i}", "err": None} for i in range(start, max(-1, start - 1000), -1)]

    r = R(2500)
    first = r.oldest_signatures("addr", 5, max_pages=5)
    assert [x["signature"] for x in first] == ["s0", "s1", "s2", "s3", "s4"] and r.calls == 3
    assert R(10_000).oldest_signatures("addr", 5, max_pages=3) is None   # too deep: skipped, not guessed


def test_pump_bonding_curve_pda():
    from memedesk.sources.solana_util import PUMP_PROGRAM, find_program_address
    # known pump.fun program accounts
    assert find_program_address([b"global"], PUMP_PROGRAM)[0] == "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    assert find_program_address([b"__event_authority"], PUMP_PROGRAM)[0] == "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
