"""Desk configuration. Every threshold the desk trades on lives here.

Values come from environment variables (or a `.env` file in the working
directory). Defaults implement the desk spec:

  scout    pairs 30m-72h old, mcap $300k-$20M (DexScreener)
  filter   kill: mint/freeze authority not revoked, LP not burned/locked,
           liquidity < 25% of mcap, dev > 3% or past rugs, bundled launch > 10%,
           already +80% in 1h
  analyst  score 0-100, only >= 70 passes
  executor max 8% of equity per trade
  risk     stop -6% set before entry, trailing stop once +12%
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

# Hard ceiling that config cannot raise. The executor and risk agents both
# enforce it independently.
HARD_MAX_POSITION_PCT = 8.0


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class Config:
    # ---- mode -------------------------------------------------------------
    mode: str = "paper"                      # paper | live
    live_confirm: str = ""                   # must equal I_UNDERSTAND_REAL_MONEY for live
    paper_start_usd: float = 1000.0
    data_dir: str = "data"

    # ---- scout ------------------------------------------------------------
    min_age_min: float = 30.0
    max_age_h: float = 72.0
    min_mcap: float = 300_000.0
    max_mcap: float = 20_000_000.0
    scout_max_candidates: int = 60           # per cycle, after age/mcap gate
    use_geckoterminal_discovery: bool = True

    # ---- filter -----------------------------------------------------------
    min_lp_locked_pct: float = 95.0          # LP burned + locked, % of LP supply
    min_liq_mcap_pct: float = 25.0           # kill if liquidity < this % of mcap
    max_dev_pct: float = 3.0                 # kill if creator holds > this % of supply
    max_bundle_pct: float = 10.0             # kill if > this % of supply bundled at launch
    max_h1_change_pct: float = 80.0          # kill if already up > this in 1h
    fail_closed: bool = True                 # unknown safety data => kill

    # ---- analyst ----------------------------------------------------------
    min_score: float = 70.0
    w_momentum: float = 20.0
    w_unique_buyers: float = 20.0
    w_buy_sell: float = 15.0
    w_holder_growth: float = 15.0
    w_liq_mcap: float = 15.0
    w_smart_wallets: float = 15.0
    smart_wallets_file: str = "smart_wallets.txt"

    # ---- executor ---------------------------------------------------------
    position_pct: float = 8.0                # of equity; clamped to HARD_MAX_POSITION_PCT
    max_pool_impact_pct: float = 1.0         # size <= this % of pool liquidity
    min_trade_usd: float = 10.0
    slippage_bps: int = 300                  # paper fill haircut / live max slippage
    paper_fee_bps: int = 30
    max_price_drift_pct: float = 3.0         # re-quote moved more than this since scoring => kill
    sol_reserve: float = 0.05                # SOL kept for fees (live)

    # ---- risk -------------------------------------------------------------
    stop_pct: float = 6.0
    trail_activate_pct: float = 12.0
    trail_pct: float = 6.0                   # trail distance below the high-water mark
    max_open_positions: int = 5
    daily_loss_limit_pct: float = 15.0       # halt new entries past this drawdown in a UTC day
    liq_crash_exit_pct: float = 50.0         # exit if pool liquidity falls this % from entry
    max_hold_h: float = 48.0                 # 0 disables
    reentry_cooldown_h: float = 24.0

    # ---- loop -------------------------------------------------------------
    scan_interval_s: float = 90.0
    monitor_interval_s: float = 10.0
    kill_cooldown_min: float = 30.0          # how long a killed mint is skipped
    score_kill_cooldown_min: float = 10.0    # analyst kills retry sooner
    report_every_h: float = 6.0              # Grok/Telegram desk report cadence

    # ---- data sources -----------------------------------------------------
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    rugcheck_url: str = "https://api.rugcheck.xyz/v1"
    dexscreener_url: str = "https://api.dexscreener.com"
    geckoterminal_url: str = "https://api.geckoterminal.com/api/v2"
    trench_url: str = "https://trench.bot/api"
    jupiter_url: str = "https://lite-api.jup.ag/swap/v1"
    http_timeout_s: float = 15.0

    # ---- grok / notifications ----------------------------------------------
    xai_api_key: str = ""
    grok_model: str = "grok-4"
    grok_url: str = "https://api.x.ai/v1"
    grok_veto: bool = False                  # let Grok veto a 70+ setup (never forces a buy)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ---- live wallet ------------------------------------------------------
    wallet_keypair_path: str = ""            # solana-keygen JSON file (live only)

    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Config":
        _load_dotenv(Path(env_file))
        cfg = cls()
        for f in fields(cls):
            if f.name == "extra":
                continue
            raw = os.environ.get(f"DESK_{f.name.upper()}")
            if raw is None and f.name in {"xai_api_key", "telegram_bot_token", "telegram_chat_id"}:
                raw = os.environ.get(f.name.upper())
            if raw is None:
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
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError(f"DESK_MODE must be paper or live, got {self.mode!r}")
        if self.mode == "live" and self.live_confirm != "I_UNDERSTAND_REAL_MONEY":
            raise ValueError(
                "Live mode needs DESK_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY. Run paper first."
            )
        if self.mode == "live" and not self.wallet_keypair_path:
            raise ValueError("Live mode needs DESK_WALLET_KEYPAIR_PATH")
        if self.position_pct <= 0:
            raise ValueError("position_pct must be > 0")
        self.position_pct = min(self.position_pct, HARD_MAX_POSITION_PCT)
        if self.stop_pct <= 0 or self.trail_pct <= 0:
            raise ValueError("stop_pct and trail_pct must be > 0")
        if self.min_age_min / 60 >= self.max_age_h or self.min_mcap >= self.max_mcap:
            raise ValueError("scout window is empty")

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
