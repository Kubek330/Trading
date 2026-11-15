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

`DESK_PROFILE=fast` (set in `.env.example`) is like active, but buys sooner:
- It doesn't wait 10 minutes to measure holder growth; a coin seen for the first time is scored on the other factors.
- The minimum score is 50.
- It scans every 60 s, and rejected coins are re-checked after 3 minutes.
- The safety filters are the same as in active, except that the liquidity/mcap floor is 3%.
- The sniper entry gate is looser: mcap ≥ $7k, ≥ 15 trades in 5 min, buy/sell ≥ 1.2, skips coins up more than 100% in 5 min.

`DESK_PROFILE=strict` is the original spec: every unknown kills, and the minimum score is 70. Any `DESK_*` value you set yourself overrides the profile.

If nothing gets bought:
- Run `python -m memedesk doctor`. It tests every data source from your phone and tells you which failure blocks trading.
- Check the **funnel** card on the dashboard. It shows, for the last hour, how many coins were scouted, passed the safety filter, scored high enough, and were bought, plus the most common kill reasons.

## Extra filters and keys

- **Jupiter organic score** (`JUP_API_KEY`, free at portal.jup.ag): Jupiter's 0–100 estimate of how much of a token's trading comes from real wallets rather than wash trading or bots. A token below 30 is killed. The check runs before the RugCheck/RPC calls, and one request scores the whole batch. If Jupiter has no score for a token, it isn't killed unless `DESK_ORGANIC_FAIL_CLOSED=true`.
- **CoinGecko Demo** (`COINGECKO_API_KEY`): the free plan is capped at **10,000 calls a month**, which an always-on bot would use up within a day. So it's used only as a backup: when the public GeckoTerminal API returns 429, the same request goes to CoinGecko's onchain API instead. The monthly count is stored, the bot stops using the key at `DESK_COINGECKO_MONTHLY_CAP` (default 9,500), and the dashboard footer shows how much has been used.

## Loss control (all books)

- **Entry confirmation:** just before buying, the executor re-reads the last 5 minutes. It won't buy while the price is down more than 3% or while sellers outnumber buyers (when there are at least 10 trades); it re-checks after 2 minutes.
- **Dump exit:** if a position is below entry and, over the last 5 minutes, the price is down 10% or more and sells are at least 2× buys, it exits at once instead of waiting to gap through the hard stop. Turn it off with `DESK_DUMP_EXIT=false`.
- **Fast profile:** stops are checked every 5 s instead of every 15 s.
- **"What works" card:** win rate and average return per book, per analyst-score band, and per exit reason, from closed trades only.

## Free data sources

- **Jupiter top lists:** each scan adds Jupiter's top-traded (5 min) and top-trending (1 h) Solana tokens to discovery, i.e. what trading apps show as hot right now. One cached call each; with a key it also fills the organic-score and audit cache.
- **PumpPortal live feed:** a free WebSocket that pushes every new pump.fun token and every graduation within about 1 s. The desk runs it in a background thread with a stdlib-only WebSocket client that reconnects on its own. It only adds mints to discovery; prices and safety still come from the normal pipeline. Turn it off with `DESK_PUMPPORTAL=false`. `doctor` checks it.
- **Jupiter Tokens V2:** besides the organic score, its `audit` fields (top holders %, dev %, mint/freeze status) and holder count fill in for RugCheck when RugCheck or the RPC doesn't answer, so coins aren't killed as "unknown". A token Jupiter flags `isSus` is killed. A free key from portal.jup.ag (`JUP_API_KEY`) is recommended, because the keyless lite-api is being throttled ahead of retirement.
- **PumpSwap pools:** pump.fun burns the liquidity pool tokens it creates at migration, so a PumpSwap pool of a pump.fun mint counts as LP-burned when RugCheck has no data.
- **Helius free tier:** setting `HELIUS_API_KEY` replaces the slow public RPC for both the filter and the whale scout.
- **Stops first:** the risk monitor fetches prices over its own DexScreener lane, so discovery traffic never delays a stop or exit.
- **Dead hosts:** after 5 consecutive network failures a source is skipped for 10 minutes instead of being waited on for every coin.

## Fake-volume check

Many fresh memecoins wash-trade, meaning a few wallets trade with each other so the coin looks busy. Before the Analyst scores a coin, and before the Sniper buys one, the desk reads its wallet-level trade tape from GeckoTerminal: the last 60 minutes for the Analyst, the last 15 for the Sniper. It kills the coin as `fake_volume` when any of these is true:
- fewer than 1 in 4 trades comes from a distinct wallet;
- the top 3 wallets make more than 60% of the volume;
- wallets that buy and sell repeatedly make more than 40% of the volume.

At least 20 trades are needed to judge. The thresholds are the `DESK_WASH_*` settings.

## Sniper (separate experiment)

The sniper hunts **brand-new coins, 3–30 minutes old**: the ones the main desk rejects as too young. It trades its own $1000 paper book, shown on the **Sniper** tab, so its results never mix with the scored or random books.
- **Tracking:** every young coin Scout sees is re-checked each scan until it turns 30 min old.
- **Entry gate:** mcap $30k–$3M, liquidity ≥ $10k, ≥ 20 trades in the last 5 min, buyers ≥ 1.3× sellers. It skips a coin that is falling, and one that is already up more than 60% in 5 min.
- **Safety:** the same on-chain checks as the main filter, with thresholds for new coins: top-10 ≤ 30%, dev ≤ 5%, bundle ≤ 15%. There is no 1-hour pump rule, since these coins are only minutes old.
- **Discovery:** besides DexScreener, Scout reads GeckoTerminal's 5-minute trending pools, the first two pages of new pools, and the most active pump.fun pools. Most launches never pay for a DexScreener profile, so the profile feed alone misses them.
- **Paper realism:** size is capped at 5% of the estimated curve reserves, and fills use 4% slippage each way instead of 2%.
- **Exits:** 4% per trade, stop −20%, at +50% sell 75%, the runner trails 30% below its peak (never below entry), max hold 6 h.
- **Turning it off:** set `DESK_SNIPER=false`. Every threshold is a `DESK_SNIPER_*` setting.

## Momentum (separate experiment)

A "buy what's rising" book, the opposite of the strict main desk. It has its own $1000 paper book and its own tab.
- **Universe:** every coin Scout saw that is 3 min – 72 h old and $10k – $20M mcap, including coins under the main desk's $300k floor, plus GeckoTerminal's most-traded pools. Momentum keeps its own list of up to 300 coins and re-checks each one every scan for 6 h. When the list is full it drops the least-traded coins, not the oldest, so a flood of new dead launches can't push out a coin that is starting to move, so a coin that starts moving between scans is caught. The re-check costs a few DexScreener calls (30 coins per call).
- **Entry:** up over 5 min (but not more than 20%) and over 1 h (but not more than 150%), at least 10 trades in 5 min, buyers ≥ 1.2× sellers, and a momentum score (buy pressure, activity, 5-min rise) of at least 40.
- **Several positions, not one:** it aims to hold 4 coins at once (max 6). Below the target it fills several slots in one scan, strongest first, but only with coins that clear every bar; a free slot is never a reason to buy a weak coin. A position that has gone nowhere for 40 min (between −5% and +8%) and has lost its momentum (falling over 5 min, or sellers ahead) is rotated out (`stale_rotate`) to free the money. Runners after take-profit are left alone.
- **Wick-proof stop:** the hard stop (−12%) sells only after 2 price checks in a row below it, about 5–10 s, so a one-candle wick that snaps back doesn't throw the position out. A price 8% or more under the stop sells at once, because that looks like a real dump. The dump exit is unchanged.
- **Re-entry:** after an exit, Momentum may buy the same coin again after 10 min (other books wait 24 h), if it passes every entry rule again.
- **Buy back in:** after selling 75%, if the coin keeps rising (price at least 10% above our last sell, still up over 5 min but not more than 20%, buyers ≥ 1.2× sellers, at least 10 trades, and not mid-spike), Momentum adds half the original size with a tighter −8% stop. At most 2 add-ons per coin, one at a time.
- **Spike exit:** the desk records each position's price every monitor tick. If the price jumps 15% or more inside 3 minutes while the position is at least 5% in profit, it sells 75% into the spike and keeps 25% as a runner with its stop at entry or higher. If the runner is all that is left, a spike instead pulls its stop up to 8% under the price.
- **Sideways exit:** after 20 min without a real run (never more than +10% above entry), if the position is at least +1% in profit after exit slippage and fees, it sells 75% and keeps 25% as a runner with its stop at entry. A sideways position at a loss is not sold early.
- **Trending lists:** a coin on a GeckoTerminal trending list gets +10 on its momentum score, which can tip a borderline coin but never buys one on its own. Trending lists lag and DexScreener rankings are partly paid boosts, so live buy pressure stays the main signal.
- **Safety:** hard checks only: mint/freeze authority, LP lock (or bonding curve), not rugged, top-10 ≤ 35%, dev ≤ 10%. **No** analyst score, holder-growth wait, organic score or fake-volume check.
- **Size:** 5% of the book ($50 of $1000), capped at 5% of pool liquidity or estimated curve reserves. A coin too thin for a $25+ position is skipped (`too_thin`) instead of bought for a few dollars.
- **Exits:** 5% per trade, stop −12%, at +25% sell 75%, the runner trails 12% below its peak (floor at entry; 8% after a spike), max hold 4 h.
- **Own loop:** Momentum re-checks its watchlist every 60 s in its own thread, so a slow full scan (filter, analyst, whale scout) never delays it.
- **Turning it off:** set `DESK_MOMENTUM=false`. Thresholds are the `DESK_MOM_*` settings.

Comparing Momentum with the Desk book on the dashboard shows whether buying often beats being picky, after slippage and fees.

## AI book (separate experiment)

An LLM trades its own $1000 paper book. It is off until `GEMINI_API_KEY` is set; a free key comes from aistudio.google.com.
- **Every minute** it sees the 10 most-traded coins from the momentum watchlist and its own open positions. For each coin it gets live DEX data (age, mcap, liquidity, price changes, buys/sells, volume) and the on-chain safety facts (mint/freeze authority, LP lock, top-10 holders, dev holdings, past rugs, bundles, holder count, Jupiter organic score and suspicious flag).
- **It decides alone:** which coins to buy, how much (1–8% of the book), and which positions to sell. No filter blocks it; it is told what looks like a scam and has to notice that itself. Every decision is logged with its reason on the AI tab.
- **Code still does** the simulated fills (same slippage and fees as the other books), the 8% hard cap, an emergency stop at −35% and a 6 h max hold, because the AI only looks once a minute. Set `DESK_AI_STOP_PCT=99` to turn the stop off.
- **Privacy:** only public coin data is sent; free-tier Gemini may use prompts to improve Google's models.
- **Switching models later** (e.g. to a paid Claude model) is a change of the client class in `memedesk/sources/llm.py`; the book itself stays the same.

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

## Windows

Double-click `scripts\run_windows.bat`, or run it from cmd. It starts the desk and restarts it automatically if it crashes; crashes are noted in `data\crashes.log`. Ctrl+C (then Y) stops it, and so does closing the window.

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
python -m memedesk why                 # top kill reasons per book in the last hour (--hours 6 for more)
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

There are 87 offline tests. They cover every filter kill rule (including top-10 holders with the pool vault excluded), the 8% cap, the −15% stop, the +35% partial sell, the runner trailing stop and its floor at entry, the 48h max hold, the Random arm's matched draws from filter survivors, handoff enforcement, the comparison verdicts, the dashboard server, HTTP backoff and caching, a check that only the standard library is imported, and Whale Scout: winner detection, wallet scoring/exclusions/add-drop reasons, whale-mode signals going through Filter/Analyst/Risk, both transaction parsers, signature paging, and pump.fun bonding-curve derivation.
