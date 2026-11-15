# Rules changelog

Every trading setting is hashed into a short **rule version** (shown when the desk starts, on the
dashboard footer, in each BUY decision and on each position). `python -m memedesk evaluate` groups
results by version, so a book is only ever judged against trades made under the same rules.

The hash depends on your `.env` too (profile, thresholds), so it differs between machines with
different settings. Write the version the desk prints at start next to each entry below.

**How to use it:** freeze the rules, let a version collect 100+ closed trades per book, then change
one thing at a time and add an entry here: date, version, what changed, why.

| Date | Version | Change | Why |
|------|---------|--------|-----|
| 2026-11-15 | (printed at start) | Measurement layer: rule versions, candidate forward returns, price paths, shadow exits, `evaluate`. Token-2022 veto. Insider-sell exit. AI book: call every 5 min, code sizes 2-4%, AI gives stop/take-profit/time limit, sees its last 10 trades, backs off on 429. | Judge rules on data instead of single charts; close known rug paths; make the AI book cheaper and harder to blow up. |
| before 2026-11-15 | (old) | Momentum: -12% stop with 2-tick confirm, spike sell, +25% take 75%, 12% runner trail, chop exit after 20 min, stale rotation after 40 min, re-adds, wash check, loss cooldown 60 min, max 3 entries/coin/24h, m5 <= +20%, h1 <= +150%. | Live-session fixes (CYBERLEEK rug, `bill` churn). Positions from this period show version "(old)". |
