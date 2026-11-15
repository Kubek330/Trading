"""Desk configuration. Every threshold the desk trades on lives here.

Values come from environment variables (or a `.env` file in the working
directory). Defaults implement the desk spec:

  scout    pairs 30m-72h old, mcap $300k-$20M (DexScreener)
  filter   kill: mint/freeze authority not revoked, LP not burned/locked,
           top-10 holders > 25%, liquidity < 5% of mcap, dev > 3% or past rugs,
           bundled launch > 10%, already +80% in 1h
  analyst  score 0-100, only >= 70 passes
  whale    every 30 min: find 5x+ winners (7d), read their earliest buyers on-chain,
           keep the top 50 wallets; whale mode sends 2+ tracked wallets buying the same
           token within 10 min straight to the filter
  random   control arm: skips the analyst, picks filter survivors at random
  executor max 8% of the book's equity per trade
  risk     stop -15% set before entry; at +35% sell 75%; the rest trails 35%
           from its peak, never below entry; max hold 48h

Paper trading only. There is no live trading code in this version.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

# Profiles only change defaults; any DESK_* variable you set still wins.
#   strict: every unknown safety datum kills, score >= 70 (the original spec)
#   active: unknown bundle / dev-history data no longer kills (authorities, LP,
#           top-10 and dev holdings stay fail-closed), score >= 60
PROFILES = {
    "strict": {"min_score": 70.0},
    "active": {"min_score": 60.0},
    # fast: like active, but buys sooner: no 10-min wait for holder growth on first sighting,
    # score >= 55, scans every 60s, rejected coins re-checked after 3 min. Safety filters unchanged.
    "fast": {"min_score": 50.0, "scan_interval_s": 60.0, "score_kill_cooldown_min": 3.0,
             "holder_growth_optional": True, "min_liq_mcap_pct": 3.0,
             "sniper_min_liq": 5_000.0, "sniper_min_txns_m5": 15,
             "sniper_min_buy_sell_m5": 1.2, "sniper_max_m5_pct": 100.0, "monitor_interval_s": 5.0,
             "sniper_min_mcap": 7_000.0},
}

# Hard ceiling that config cannot raise. Executor and risk enforce it independently.
HARD_MAX_POSITION_PCT = 8.0


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.split(" #")[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def _bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class Config:
    profile: str = "strict"                  # Config.load() defaults to "active"; see PROFILES
    paper_start_usd: float = 1000.0          # per book (scored and random each get this)
    data_dir: str = "data"

    # ---- scout ------------------------------------------------------------
    min_age_min: float = 30.0
    max_age_h: float = 72.0
    min_mcap: float = 300_000.0
    max_mcap: float = 20_000_000.0
    scout_max_candidates: int = 40           # per cycle, after the age/mcap gate
    watch_recheck_per_cycle: int = 150       # watchlist coins re-checked per scan (30 per DexScreener call)
    use_geckoterminal_discovery: bool = True

    # ---- filter -----------------------------------------------------------
    min_lp_locked_pct: float = 95.0          # LP burned + locked, % of LP supply
    max_top10_pct: float = 25.0              # kill if top-10 holders (ex pools) own > this %
    min_liq_mcap_pct: float = 5.0            # kill if liquidity < this % of mcap
    max_dev_pct: float = 3.0
    max_bundle_pct: float = 10.0
    max_h1_change_pct: float = 80.0
    fail_closed: bool = True                 # unknown safety data => kill
    min_organic_score: float = 30.0          # Jupiter organicScore 0-100; kill below (0 disables)
    organic_fail_closed: bool = False        # Jupiter has no score for the token => kill?

    # ---- fake-volume (wash trading) check, from the wallet-level trade tape ---
    wash_check: bool = True
    wash_min_trades: int = 20                # need this many trades to judge
    wash_min_unique_ratio: float = 0.25      # kill if unique wallets / trades below this
    wash_max_top3_share: float = 0.60        # kill if the top 3 wallets make more of the volume
    wash_max_roundtrip_share: float = 0.40   # kill if back-and-forth wallets make more of the volume

    # ---- analyst ----------------------------------------------------------
    min_score: float = 70.0
    w_momentum: float = 20.0
    w_unique_buyers: float = 20.0
    w_buy_sell: float = 15.0
    w_holder_growth: float = 15.0
    w_liq_mcap: float = 15.0
    w_smart_wallets: float = 15.0
    smart_wallets_file: str = "smart_wallets.txt"
    holder_growth_optional: bool = False     # True: unmeasured holder growth drops out of the score instead of scoring 0

    # ---- random control ---------------------------------------------------
    random_control: bool = True              # run the luck baseline alongside
    random_seed: int = 0                     # 0 = time-based

    # ---- whale scout ------------------------------------------------------
    whale_scout: bool = True
    whale_interval_min: float = 30.0
    whale_min_multiple: float = 5.0          # 5x+ ...
    whale_lookback_d: float = 7.0            # ... within the last 7 days
    whale_pools_per_run: int = 6             # OHLCV checks per run; GeckoTerminal is shared with Scout/Analyst
    whale_tokens_per_run: int = 0            # winners whose early buyers are read per run (0 = auto)
    whale_early_swaps: int = 0               # earliest swaps read per winner (0 = auto: 200 Helius / 80 public)
    whale_max_sig_pages: int = 0             # x1000 signatures walked back per winner (0 = auto)
    whale_top_n: int = 50
    whale_min_winners: int = 2               # must have bought >= 2 different winners early
    whale_inactive_days: float = 7.0
    whale_bot_trades_per_h: float = 50.0
    whale_mode: bool = True
    whale_signal_wallets: int = 2
    whale_signal_window_min: float = 10.0
    whale_watch_interval_s: float = 0.0      # poll tracked wallets (0 = auto: 60s Helius / 180s public)
    helius_api_key: str = ""
    pumpportal: bool = True                  # free live WebSocket feed of new pump.fun tokens + graduations
    jup_api_key: str = ""                    # free at portal.jup.ag (organic score)
    coingecko_api_key: str = ""              # CoinGecko Demo: backup when GeckoTerminal says 429
    coingecko_monthly_cap: int = 9500        # Demo plan allows 10,000 calls/month; keep a margin

    # ---- sniper (separate experiment book: brand-new coins, 3-30 min old) ----
    sniper: bool = True
    sniper_min_age_min: float = 3.0
    sniper_max_age_min: float = 30.0
    sniper_min_mcap: float = 30_000.0
    sniper_max_mcap: float = 3_000_000.0
    sniper_min_liq: float = 10_000.0
    sniper_min_txns_m5: int = 20             # real activity right now
    sniper_min_buy_sell_m5: float = 1.3      # buyers outnumber sellers
    sniper_max_m5_pct: float = 60.0          # don't buy a vertical candle
    sniper_track_max: int = 80               # young coins re-checked per scan
    sniper_max_top10_pct: float = 30.0
    sniper_max_dev_pct: float = 5.0
    sniper_max_bundle_pct: float = 15.0
    sniper_min_organic_score: float = 0.0    # brand-new tokens rarely have a Jupiter score yet
    sniper_position_pct: float = 4.0         # smaller: sniping is riskier (hard cap 8% still applies)
    sniper_max_pool_impact_pct: float = 5.0  # size <= this % of (estimated) curve reserves
    sniper_slippage_bps: int = 400           # paper slippage each way: thin curves move more than pools
    sniper_stop_pct: float = 20.0
    sniper_tp_trigger_pct: float = 50.0
    sniper_tp_sell_pct: float = 75.0
    sniper_runner_trail_pct: float = 30.0
    sniper_max_hold_h: float = 6.0
    sniper_max_open_positions: int = 5

    # ---- momentum (separate experiment book: buys often, fable60-style) -----
    momentum: bool = True
    mom_min_age_min: float = 3.0
    mom_max_age_h: float = 72.0
    mom_min_mcap: float = 10_000.0
    mom_max_mcap: float = 20_000_000.0
    mom_min_liq: float = 5_000.0             # not applied on bonding curves (DexScreener shows ~$0 there)
    mom_min_txns_m5: int = 10
    mom_min_buy_sell_m5: float = 1.2
    mom_max_m5_pct: float = 20.0             # do not buy a vertical candle (WANTED: bought after the spike, -33%)
    mom_max_h1_pct: float = 150.0
    mom_max_entries_per_scan: int = 2        # per scan when already at the target
    mom_target_positions: int = 4            # try to hold this many at once (fills up to it per scan)
    mom_min_quality: float = 40.0            # momentum score 0-100 a coin needs, free slot or not
    mom_track_max: int = 300                 # coins momentum re-checks every scan (30 per DexScreener call)
    mom_track_hours: float = 6.0             # how long a seen coin stays on momentum's list
    mom_trending_bonus: float = 10.0         # quality bonus for coins on a trending list (still must pass every rule)
    mom_stop_confirm_ticks: int = 2          # hard stop fires only after this many price checks in a row below it...
    mom_stop_gap_pct: float = 8.0            # ...unless price is this far below the stop (looks like a real dump)
    mom_reentry_cooldown_min: float = 10.0   # after an exit, may buy the same coin again this soon (still all rules)
    mom_readd: bool = True                   # after selling 75%, buy back in if it keeps rising
    mom_readd_above_pct: float = 10.0        # ...price at least this far above our last sell
    mom_readd_size_frac: float = 0.5         # ...half the original position
    mom_readd_stop_pct: float = 8.0          # ...with a tighter stop (bought higher)
    mom_max_readds: int = 2                  # ...at most this many times per coin, one at a time
    mom_spike_pct: float = 15.0              # price up this much inside the window = a spike: sell into it
    mom_spike_window_s: float = 180.0
    mom_spike_min_pnl_pct: float = 5.0       # only when the position is at least this much in profit
    mom_spike_sell_pct: float = 75.0         # sold into the spike; the rest rides with a stop >= entry
    mom_spike_runner_stop_pct: float = 8.0   # spike while only the runner is left: stop pulled to 8% under price
    mom_chop_min: float = 20.0               # after this long without a real run...
    mom_chop_max_peak_pct: float = 10.0      # ...(never more than +10% above entry)...
    mom_chop_min_net_pct: float = 1.0        # ...take a small profit (net of exit slippage + fee)...
    mom_chop_sell_pct: float = 75.0          # ...on this much, keeping the rest as a runner floored at entry
    mom_stale_min: float = 40.0              # rotate out of a position flat for this long...
    mom_stale_low_pct: float = -5.0          # ...with P&L between these bounds...
    mom_stale_high_pct: float = 8.0          # ...and its momentum gone (falling 5m or sellers ahead)
    mom_max_top10_pct: float = 35.0
    mom_max_dev_pct: float = 10.0
    mom_position_pct: float = 5.0            # $50 of a $1000 book
    mom_max_pool_impact_pct: float = 5.0     # size <= this % of pool liquidity (or estimated curve reserves)
    mom_min_trade_usd: float = 25.0          # skip coins too thin for a real position instead of buying $3
    mom_stop_pct: float = 12.0
    mom_tp_trigger_pct: float = 25.0
    mom_tp_sell_pct: float = 75.0
    mom_runner_trail_pct: float = 12.0
    mom_max_hold_h: float = 4.0
    mom_max_open_positions: int = 6

    # ---- AI book: an LLM decides buys, size and sells on its own (no filters) ----
    ai: bool = True                          # active when GEMINI_API_KEY is set
    gemini_api_key: str = ""                 # free key: aistudio.google.com -> Get API key
    ai_model: str = ""                       # empty = gemini-2.5-flash, or the newest flash model on the key
    ai_interval_s: float = 60.0              # one LLM call per minute (fits free-tier limits)
    ai_candidates: int = 10                  # most-traded coins shown to the AI per call
    ai_stop_pct: float = 35.0                # emergency stop only (the AI looks once a minute); 99 = off
    ai_max_hold_h: float = 6.0
    ai_max_open: int = 6

    # ---- executor ---------------------------------------------------------
    position_pct: float = 8.0                # of the book's equity; clamped to 8
    max_pool_impact_pct: float = 1.0         # size <= this % of pool liquidity
    min_trade_usd: float = 10.0
    slippage_bps: int = 200                  # paper fill haircut each way
    paper_fee_bps: int = 50                  # paper fee each way (DEX + priority)
    max_price_drift_pct: float = 3.0

    # ---- risk -------------------------------------------------------------
    stop_pct: float = 15.0                   # hard stop below entry, set before entry
    tp_trigger_pct: float = 35.0             # take-profit trigger
    tp_sell_pct: float = 75.0                # % of the position sold at the trigger
    runner_trail_pct: float = 35.0           # remainder trails this far below its peak
    runner_floor_at_entry: bool = True       # runner stop never below entry
    max_hold_h: float = 48.0
    max_open_positions: int = 5              # per book
    daily_loss_limit_pct: float = 20.0       # halt new entries in a book past this UTC-day drawdown
    liq_crash_exit_pct: float = 50.0         # rug guard: exit if pool liquidity halves
    dump_exit: bool = True                   # exit early on a live dump, before the hard stop
    dump_m5_pct: float = 10.0                #   ...price down >= this % over the last 5 min
    dump_sell_ratio: float = 2.0             #   ...and sells >= this x buys in the last 5 min (position below entry)
    entry_max_m5_drop_pct: float = 3.0       # don't buy while it is dropping more than this over 5 min
    entry_min_buy_sell_m5: float = 1.0       # ...or while sellers outnumber buyers (when >= 10 trades in 5 min)
    reentry_cooldown_h: float = 24.0

    # ---- loop -------------------------------------------------------------
    scan_interval_s: float = 120.0
    monitor_interval_s: float = 15.0
    kill_cooldown_min: float = 30.0
    score_kill_cooldown_min: float = 10.0
    report_every_h: float = 6.0

    # ---- data sources -----------------------------------------------------
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    rugcheck_url: str = "https://api.rugcheck.xyz/v1"
    dexscreener_url: str = "https://api.dexscreener.com"
    geckoterminal_url: str = "https://api.geckoterminal.com/api/v2"
    trench_url: str = "https://trench.bot/api"
    http_timeout_s: float = 20.0
    rpc_rpm: int = 40                        # public RPC is strict; stay well under

    # ---- dashboard --------------------------------------------------------
    dashboard: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8084

    # ---- grok / notifications ----------------------------------------------
    xai_api_key: str = ""
    grok_model: str = "grok-4"
    grok_url: str = "https://api.x.ai/v1"
    grok_veto: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Config":
        _load_dotenv(Path(env_file))
        cfg = cls()
        prof = (os.environ.get("DESK_PROFILE") or "active").strip().lower()
        if prof not in PROFILES:
            raise ValueError(f"DESK_PROFILE must be one of {sorted(PROFILES)}, got {prof!r}")
        cfg.profile = prof
        for k, v in PROFILES[prof].items():
            setattr(cfg, k, v)
        for f in fields(cls):
            if f.name == "profile":
                continue
            raw = os.environ.get(f"DESK_{f.name.upper()}")
            if raw is None and f.name in {"xai_api_key", "telegram_bot_token", "telegram_chat_id",
                                          "helius_api_key", "jup_api_key", "coingecko_api_key",
                                          "gemini_api_key"}:
                raw = os.environ.get(f.name.upper())
            if raw is None or raw == "":
                continue
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                setattr(cfg, f.name, _bool(raw))
            elif isinstance(cur, int):
                setattr(cfg, f.name, int(float(raw)))
            elif isinstance(cur, float):
                setattr(cfg, f.name, float(raw))
            else:
                setattr(cfg, f.name, raw)
        if os.environ.get("DESK_MODE", "paper").lower() != "paper":
            raise ValueError("This build is paper-only. Remove DESK_MODE or set it to paper.")
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.position_pct <= 0:
            raise ValueError("position_pct must be > 0")
        self.position_pct = min(self.position_pct, HARD_MAX_POSITION_PCT)
        if not 0 < self.stop_pct < 100 or self.runner_trail_pct <= 0:
            raise ValueError("stop_pct and runner_trail_pct must be > 0")
        if not 0 < self.tp_sell_pct <= 100 or self.tp_trigger_pct <= 0:
            raise ValueError("tp_sell_pct must be in (0, 100], tp_trigger_pct > 0")
        if self.min_age_min / 60 >= self.max_age_h or self.min_mcap >= self.max_mcap:
            raise ValueError("scout window is empty")

    def whale_auto(self, name: str) -> float:
        """Budgets that depend on whether a Helius key is available."""
        helius = bool(self.helius_api_key)
        val = getattr(self, name)
        if val:
            return val
        return {"whale_tokens_per_run": (20, 3), "whale_early_swaps": (200, 80),
                "whale_max_sig_pages": (40, 6), "whale_watch_interval_s": (60, 180)}[name][0 if helius else 1]

    @property
    def books(self) -> list[str]:
        return (["scored"] + (["random"] if self.random_control else []) + (["sniper"] if self.sniper else [])
                + (["momentum"] if self.momentum else []) + (["ai"] if self.ai and self.gemini_api_key else []))

    def rules(self, book: str) -> "Config":
        """Exit/sizing rules for a book. Sniper and momentum have their own; scored and random share the main ones."""
        from dataclasses import replace
        if book == "ai":
            return replace(self, stop_pct=self.ai_stop_pct, tp_trigger_pct=1e9, max_hold_h=self.ai_max_hold_h,
                           max_open_positions=self.ai_max_open, position_pct=HARD_MAX_POSITION_PCT,
                           liq_crash_exit_pct=100.0, max_pool_impact_pct=self.mom_max_pool_impact_pct,
                           min_trade_usd=self.min_trade_usd)
        if book == "momentum":
            return replace(self, stop_pct=self.mom_stop_pct, tp_trigger_pct=self.mom_tp_trigger_pct,
                           tp_sell_pct=self.mom_tp_sell_pct, runner_trail_pct=self.mom_runner_trail_pct,
                           max_hold_h=self.mom_max_hold_h, position_pct=min(self.mom_position_pct, HARD_MAX_POSITION_PCT),
                           max_open_positions=self.mom_max_open_positions,
                           max_pool_impact_pct=self.mom_max_pool_impact_pct, min_trade_usd=self.mom_min_trade_usd)
        if book != "sniper":
            return self
        return replace(self, stop_pct=self.sniper_stop_pct, tp_trigger_pct=self.sniper_tp_trigger_pct,
                       tp_sell_pct=self.sniper_tp_sell_pct, runner_trail_pct=self.sniper_runner_trail_pct,
                       max_hold_h=self.sniper_max_hold_h, position_pct=min(self.sniper_position_pct, HARD_MAX_POSITION_PCT),
                       max_open_positions=self.sniper_max_open_positions,
                       max_pool_impact_pct=self.sniper_max_pool_impact_pct, slippage_bps=self.sniper_slippage_bps)

    @property
    def score_weights(self) -> dict[str, float]:
        return {
            "momentum": self.w_momentum,
            "unique_buyers": self.w_unique_buyers,
            "buy_sell": self.w_buy_sell,
            "holder_growth": self.w_holder_growth,
            "liq_mcap": self.w_liq_mcap,
            "smart_wallets": self.w_smart_wallets,
        }
