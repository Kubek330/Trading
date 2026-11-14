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
    "fast": {"min_score": 55.0, "scan_interval_s": 60.0, "score_kill_cooldown_min": 3.0,
             "holder_growth_optional": True},
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
    whale_pools_per_run: int = 12            # OHLCV checks per run; GeckoTerminal is shared with Scout/Analyst
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
    jup_api_key: str = ""                    # free at portal.jup.ag (organic score)
    coingecko_api_key: str = ""              # CoinGecko Demo: backup when GeckoTerminal says 429
    coingecko_monthly_cap: int = 9500        # Demo plan allows 10,000 calls/month; keep a margin

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
                                          "helius_api_key", "jup_api_key", "coingecko_api_key"}:
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
        return ["scored", "random"] if self.random_control else ["scored"]

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
