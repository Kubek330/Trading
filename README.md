# memedesk

A six-agent Solana memecoin desk that runs unattended overnight. It hunts fresh coins using live on-chain and DEX data, kills anything unsafe, scores what's left, and paper-trades the strongest. Every trade has a stop in place before entry. Grok (xAI) is the desk's voice: it gives a second opinion on setups and writes the morning report.

```
 scout ──► filter ──► analyst ──► executor ──► risk ──► executor ──► [position]
 (window)  (kills)    (0-100,     (size ≤8%)   (stop     (fill)          │
                       ≥70 only)               -6% set)                  ▼
                                         risk monitor every 10s: stop / trail / liq-crash
 logger: every handoff, kill, fill, stop move → data/desk_<mode>.db + .jsonl
```

**Hard handoffs.** Each agent accepts one typed object and returns either the next type or a `Kill`. Every object carries a `trail` of the agents that have handled it, and each agent refuses input that skipped the stage before it. The executor will not fill a ticket unless Risk has issued an approval for that exact ticket ID with a stop attached.

## The agents

| Agent | Does | Data |
|---|---|---|
| **scout** | Finds Solana pairs **30m–72h old** with **mcap $300k–$20M** (deepest SOL/USDC pool per token) | DexScreener (profiles, boosts, `/tokens/v1`), GeckoTerminal trending/new pools for extra discovery |
| **filter** | Kills a coin if: mint authority isn't revoked · freeze authority isn't revoked · LP isn't burned or locked (≥95%) · liquidity is under 25% of mcap · dev holds more than 3% · dev has past rugs · launch was more than 10% bundled · coin is already up more than 80% in 1h · RugCheck marks it rugged | Solana RPC `getAccountInfo` (authorities, read on-chain) · RugCheck report (LP, creator, holders, rug history) · trench.bot bundle analysis (falls back to RugCheck insiders) |
| **analyst** | Scores 0–100 from momentum (20), unique buyers (20), buy/sell ratio (15), holder growth (15), liq/mcap (15) and smart wallets (15). **Only scores of 70 or more pass.** If a data point is missing, that part scores 0. Grok can add a note, or a veto if you turn that on | GeckoTerminal trade tape (buyer wallets, USD flow) · DexScreener txns · our own holder snapshots |
| **executor** | Re-quotes the price, kills the trade if the price has drifted more than 3% or the coin is now up more than 80% in 1h, and sizes the position at **≤8% of equity** (capped in code, so config can't raise it), ≤1% of pool liquidity and ≤ cash | Paper broker or Jupiter (live) |
| **risk** | Before entry: sets the stop at **-6%**, checks max open positions, skips mints already held, and halts new entries after a 15% daily drawdown. While a position is open: arms a **trailing stop once it's up 12%** (6% below the peak), exits on a liquidity crash (50% drop) or after 48h, and blacklists the creators of rugged coins | DexScreener prices every 10s |
| **logger** | Logs **every kill** (agent, reason code, detail, data), every handoff, fill, exit and stop move. Holds positions and account state, so a restart picks up where it left off | SQLite + JSONL |

## Run it (paper first)

```bash
pip install -r requirements.txt
cp .env.example .env              # optional: XAI_API_KEY, TELEGRAM_*, a paid RPC URL
cp smart_wallets.example.txt smart_wallets.txt   # add wallets you trust

python -m memedesk once           # one scan cycle, to check that data flows
./scripts/run_overnight.sh        # background loop with auto-restart
tail -f data/overnight.log
```

In the morning:

```bash
python -m memedesk report --hours 12   # Grok writes the brief if XAI_API_KEY is set
python -m memedesk positions
python -m memedesk kills -n 100        # every kill, with its reason
sqlite3 data/desk_paper.db "select agent, reason, count(*) from kills group by 1,2 order by 3 desc"
```

On a VPS, use `deploy/memedesk.service` (systemd). With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set, entries, exits and the reports (every 6h) go to your phone.

## Going live

Only after the paper results hold up over several nights. All three settings are required:

```
DESK_MODE=live
DESK_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY
DESK_WALLET_KEYPAIR_PATH=~/.config/solana/desk-hot-wallet.json   # a dedicated hot wallet
```

Live mode swaps through Jupiter, signs the transaction locally and sends it over your RPC. Fund the hot wallet with only what you're willing to lose. Paper and live keep separate databases.

## Things to know

- **The "liq 25%" rule is read as liquidity ≥ 25% of mcap.** That's strict: many $2M+ coins sit at 8–15%, so expect this to be the top kill reason. If you meant something else (for example, top-10 holders ≤ 25%), tell me. If you only want to loosen it, change `DESK_MIN_LIQ_MCAP_PCT`.
- **Holder growth needs 10+ minutes of observation.** On first sighting that part scores 0. The desk snapshots holder counts, and analyst kills retry after 10 min, so a strong coin usually passes on its second look.
- **Stops are polled, not on-chain orders.** The desk checks prices every 10s, so a coin that gaps down can fill below -6%. Paper mode books the exit at the price it actually saw, not at the stop price.
- **Paper fills** use the DexScreener price plus 3% slippage plus 0.3% fees each way. Live slippage on thin pools can be worse.
- The public Solana RPC rate-limits hard, so use a Helius/QuickNode/Triton URL for overnight runs.
- `DESK_FAIL_CLOSED=true` (the default) kills any coin whose safety data can't be fetched. Don't turn it off for live trading.
- This is a tool, not financial advice. Most fresh memecoins go to zero.

## Tests

```bash
python -m pytest -q
```

The tests run the whole pipeline offline against fake data sources: every filter kill rule, the 8% cap, the -6% stop, the trailing stop, the liquidity-crash exit, the daily halt, enforcement of the handoff order, and the guard on live mode.
