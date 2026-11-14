# memedesk

A paper-trading Solana memecoin desk that runs overnight on an Android phone (Termux) or any machine with Python 3.10+. It hunts fresh coins using live on-chain and DEX data, kills anything unsafe, and scores what's left. A **Random control** book trades the same filtered coins without scoring, so you can see whether the score beats luck. You can watch everything at **http://127.0.0.1:8084**.

The dashboard is dark, terminal-style and built for a 390px phone screen. It has:
- a ticker of the tokens the desk is watching, with 1h % change
- the book balance, with amount put in, trade count, multiplier, PnL, wins/losses, and an equity chart with a dashed "put in" line
- agent chips that show live status
- a **Pipeline** tab: one card per agent, showing status and last action, plus the tracked wallets
- positions with MC and entry → now prices
- a decision stream (BUY/SELL with the reason)
- a **Random** tab showing the control book and the comparison

Every number on it comes from the desk's own records.

It is paper only: there is no live trading code in this version. It needs no third-party packages, only the standard library.

```
 whale scout (every 30 min) ─ 5x winners → earliest buyers on-chain → top 50 wallets → smart_wallets.txt
      └─ whale mode: 2+ tracked wallets buy the same token within 10 min ──────┐
                                                                               ▼
                       ┌─► analyst (score ≥70) ─► executor ─► risk (stop set) ─► executor  [scored book]
 scout ─► filter ──────┤
 (window)  (kills)     └─► random (same count)  ─► executor ─► risk (stop set) ─► executor  [random book]

 risk monitor, every 15s, both books, same exits ─► executor (sell)
 logger: every handoff, kill, fill, partial sell, stop move ─► data/desk_paper.db + .jsonl
```

**Hard handoffs.** Each agent accepts one typed object and returns either the next type or a `Kill`. Every object carries a `trail` of the agents that handled it, and an agent refuses input that skipped a stage. The executor fills only a ticket that has a matching Risk approval with the stop already attached.

## Agents

| Agent | Does |
|---|---|
| **whale scout** | Every 30 min it finds Solana tokens that ran **5x+ in the last 7 days**, using GeckoTerminal 4h candles: peak high divided by the lowest low before the peak. For each winner it reads the **first ~200 swaps from chain**. For pump.fun mints it reads the bonding curve (address derived on-device); otherwise it reads the pool. It uses Helius if `HELIUS_API_KEY` is set, otherwise the public RPC with a small per-run budget, and caches every parsed swap. Wallets are scored on how many different winners they bought early, plus the realized SOL PnL from every swap of theirs the desk has seen. It skips bots and MEV (more than 50 trades in an hour, or a buy and sell in one slot), RugCheck creators and insider-flagged wallets, and wallets inactive for 7 days. It keeps the **top 50** in `smart_wallets.txt` and logs every add and drop with its reason. **Whale mode:** it polls the tracked wallets, and when **2+ of them buy the same token within 10 min**, the token goes straight to Filter. It must still pass Filter, Analyst 70+ and Risk. |
| **scout** | Finds Solana pairs **30m–72h old** with **mcap $300k–$20M**. Pair data comes from DexScreener; discovery also uses DexScreener profiles/boosts and GeckoTerminal's trending and new pools. |
| **filter** | Kills a coin if: **mint or freeze authority isn't revoked** (read on-chain via RPC) · **LP isn't burned or locked** (≥95%) · **top-10 holders own more than 25%** (pool and AMM vaults excluded) · liquidity is **under 5% of mcap** · **dev holds more than 3%** or has **past rugs** · launch was **more than 10% bundled** · coin is **already up more than 80% in 1h** · coin is marked rugged. If any of this data can't be fetched, the coin is killed. |
| **analyst** | Scores 0–100 from momentum (20), unique buyers (20), buy/sell ratio (15), holder growth (15), liq/mcap (15) and smart wallets (15). **Only scores of 70 or more pass.** If a data point is missing, that part scores 0. |
| **random** | The luck baseline. It **skips the Analyst**. Each cycle it draws as many coins as the Analyst passed, uniformly at random from the same cycle's filter survivors, which includes coins the Analyst rejected. They go into a separate $1000 book. |
| **executor** | Re-quotes the price and sizes the position at **≤8% of that book's equity** (capped in code), ≤1% of pool liquidity and ≤ cash. It fills only with a Risk approval. Paper fills include 2% slippage and 0.5% fees each way. |
| **risk** | Applies the same exits to both books: **stop −15%**, set before entry · **at +35%, sell 75%** · the remaining **25% trails 35% below its peak, never below entry** · **max hold 48h**. It also has a rug guard (exit if pool liquidity halves or the pool disappears), a cap of 5 open positions per book, and a 20% daily-drawdown halt per book. |
| **logger** | Logs **every kill** (agent, book, reason code, detail, data), every handoff, fill, partial sell and stop move. Holds positions and per-book cash, so a restart resumes where it stopped. |

## Profiles: why it trades (or doesn't)

`DESK_PROFILE=active` is the default.
- If a secondary data source (trench.bot bundles, creator rug history) is down or has no data for a coin, the coin is no longer killed.
- The minimum score drops to 60.
- Mint/freeze authority, LP lock, top-10 holders and dev holdings stay fail-closed in every profile.

`DESK_PROFILE=strict` is the original spec: every unknown kills, and the minimum score is 70. Any `DESK_*` value you set yourself overrides the profile.

If nothing gets bought:
- Run `python -m memedesk doctor`. It tests every data source from your phone and tells you which failure blocks trading.
- Check the **funnel** card on the dashboard. It shows, for the last hour, how many coins were scouted, passed the safety filter, scored high enough, and were bought, plus the most common kill reasons.

## Extra filters and keys

- **Jupiter organic score** (`JUP_API_KEY`, free at portal.jup.ag): Jupiter's 0–100 estimate of how much of a token's trading comes from real wallets rather than wash trading or bots. A token below 30 is killed. The check runs before the RugCheck/RPC calls, and one request scores the whole batch. If Jupiter has no score for a token, it isn't killed unless `DESK_ORGANIC_FAIL_CLOSED=true`.
- **CoinGecko Demo** (`COINGECKO_API_KEY`): the free plan is capped at **10,000 calls a month**, which an always-on bot would use up within a day. So it's used only as a backup: when the public GeckoTerminal API returns 429, the same request goes to CoinGecko's onchain API instead. The monthly count is stored, the bot stops using the key at `DESK_COINGECKO_MONTHLY_CAP` (default 9,500), and the dashboard footer shows how much has been used.

## Does scoring beat luck?

The dashboard and the report compare the two books: win rate, average and median return per trade, take-profit hits, and equity. They also run a Welch t-test on per-trade returns. Until each book has **20+ closed trades**, the verdict reads "too early". After that, a t-value above 2 means the score is adding edge over the filter alone. A t-value between −2 and 2 means it hasn't shown any edge yet.

## Android (Termux)

Install Termux from F-Droid (the Play Store build is outdated). Then:

```bash
pkg install -y git python
git clone <this repo> && cd Trading
bash scripts/termux.sh setup     # installs python + termux-api, creates .env
bash scripts/termux.sh start     # takes a wake lock, runs with auto-restart
```

Open **http://127.0.0.1:8084** in the phone's browser. Use `bash scripts/termux.sh log` to follow the log and `bash scripts/termux.sh stop` to stop.

**Make it feel like an app:**
- **Home-screen icon:** with the desk running, open http://127.0.0.1:8084 in Chrome, then tap ⋮ → "Add to Home screen" / "Install app". MemeDesk opens full screen with its own icon.
- **Start/Stop buttons:** install **Termux:Widget** from F-Droid and add its widget to your home screen. `setup` creates the MemeDesk-Start / Stop / Open / Update buttons it shows.
- **Autostart after a reboot:** install **Termux:Boot** from F-Droid and open it once.
- **Update:** `bash scripts/termux.sh update` stops the desk, runs `git pull` and starts it again.

To keep it alive overnight: set Android Settings → Apps → Termux → Battery to **Unrestricted**, keep the phone charging, and leave the Termux notification in place (it holds the wake lock).

**Public RPC friendliness.** Every host is paced per minute (Solana RPC at 40/min by default). A 429 response triggers exponential backoff with jitter, honours `Retry-After`, and pauses that host for every thread. Responses are cached:
- mint authorities: forever once revoked, otherwise 10 minutes
- RugCheck reports: 5 minutes
- bundle data: 1 hour
- discovery feeds: 1–2 minutes
- trade tapes: 1 minute

The dashboard header shows request, 429 and cache-hit counts.

## Other machines

```bash
python3 -m memedesk once          # one scan cycle, to check that data flows
./scripts/run_overnight.sh        # background loop with auto-restart
```

## In the morning

```bash
python -m memedesk report --hours 12   # both books + verdict; Grok writes a brief if XAI_API_KEY is set
python -m memedesk positions
python -m memedesk kills -n 100
python -m memedesk doctor              # which data sources work from this device
python -m memedesk dashboard           # dashboard over saved data, no trading
sqlite3 data/desk_paper.db "select agent, book, reason, count(*) from kills group by 1,2,3 order by 4 desc"
```

## Things to know

- **Holder growth needs 10+ minutes of observation.** On first sighting that part scores 0. Analyst kills are retried after 10 min, so a strong coin usually passes on its second look.
- **Stops are polled every 15s.** A coin that gaps down can exit well below −15%, and paper mode books the exit at the price it actually saw.
- **The rug guard (liquidity halving) is an addition to your exit rules.** It applies to both books equally, so the comparison stays fair. To turn it off, set `DESK_LIQ_CRASH_EXIT_PCT=100`.
- **Smart wallets** come from Whale Scout's `smart_wallets.txt` plus your own `smart_wallets.manual.txt`. The Analyst reloads them when they change. Until the list has wallets, its 15 points of weight are spread over the other five parts.
- **Whale Scout on the public RPC is slow, on purpose.** Each 30-min run reads about 3 winners × 80 early swaps, and walks back at most 6,000 signatures per winner. A winner with a deeper history is logged as `too_deep` and skipped, not guessed. Over a night the wallet book fills up gradually. A free Helius key raises this to about 20 winners × 200 swaps per run, with 60s wallet polling.
- **Realized PnL** is calculated only from swaps the desk has actually read: early buys, plus later trades seen while polling. Until a wallet has a sell on record, its PnL shows as `?` and its score is capped at 80.
- The dashboard binds to 127.0.0.1 only. Don't expose it to your network.
- This is a research tool, not financial advice. Most fresh memecoins go to zero.

## Tests

```bash
pip install pytest && python -m pytest -q
```

There are 52 offline tests. They cover every filter kill rule (including top-10 holders with the pool vault excluded), the 8% cap, the −15% stop, the +35% partial sell, the runner trailing stop and its floor at entry, the 48h max hold, the Random arm's matched draws from filter survivors, handoff enforcement, the comparison verdicts, the dashboard server, HTTP backoff and caching, a check that only the standard library is imported, and Whale Scout: winner detection, wallet scoring/exclusions/add-drop reasons, whale-mode signals going through Filter/Analyst/Risk, both transaction parsers, signature paging, and pump.fun bonding-curve derivation.
