"""`python -m memedesk evaluate`: are the books any good, or is it luck?

Only uses what the desk recorded (closed positions, shadow exits, candidate forward returns).
Statistics are plain stdlib: bootstrap confidence intervals, a permutation test against the random
book, Holm correction because several books are compared at once.
"""
from __future__ import annotations

import random
import statistics
import time

from .research import HORIZONS, VARIANTS

MIN_N = 30          # below this, numbers are printed but flagged as too few to judge


def bootstrap_ci(xs: list[float], n: int = 2000, seed: int = 1) -> tuple[float, float]:
    if len(xs) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def permutation_p(a: list[float], b: list[float], n: int = 5000, seed: int = 2) -> float:
    """Two-sided p-value for 'mean(a) != mean(b)' by shuffling the labels."""
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    rng = random.Random(seed)
    obs = abs(statistics.fmean(a) - statistics.fmean(b))
    pool, na = a + b, len(a)
    hits = 0
    for _ in range(n):
        rng.shuffle(pool)
        if abs(statistics.fmean(pool[:na]) - statistics.fmean(pool[na:])) >= obs - 1e-12:
            hits += 1
    return (hits + 1) / (n + 1)


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values (NaN entries are left out of the count)."""
    items = sorted(((p, k) for k, p in pvals.items() if p == p))
    m, out, running = len(items), {k: float("nan") for k in pvals}, 0.0
    for i, (p, k) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def book_stats(rows: list[tuple[float, float]]) -> dict:
    """rows: (pnl_pct, pnl_usd) per closed position."""
    pct = [r[0] for r in rows]
    usd = [r[1] for r in rows]
    if not pct:
        return {"n": 0}
    wins, losses = sum(u for u in usd if u > 0), -sum(u for u in usd if u < 0)
    lo, hi = bootstrap_ci(pct)
    return {"n": len(pct), "mean": statistics.fmean(pct), "ci": (lo, hi), "median": statistics.median(pct),
            "pf": (wins / losses) if losses else float("inf") if wins else float("nan"),
            "win": 100 * sum(1 for x in pct if x > 0) / len(pct),
            "big_loss": 100 * sum(1 for x in pct if x <= -50) / len(pct), "usd": sum(usd)}


def _fmt(x, nd=1, sign=True):
    if x is None or x != x:
        return "  n/a"
    if x == float("inf"):
        return "  inf"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def run(logger, books: list[str], hours: float | None = None, out=print) -> dict:
    since = time.time() - hours * 3600 if hours else 0.0
    closed = [p for p in logger.positions("closed") if (p.exit_ts or 0) >= since]
    by_book: dict[str, list] = {b: [] for b in books}
    for p in closed:
        by_book.setdefault(p.book, []).append(p)

    out(f"memedesk evaluate · {'last %gh' % hours if hours else 'all time'} · closed positions only\n")
    out(f"{'book':9} {'n':>4} {'mean%':>7} {'95% CI':>17} {'median':>7} {'PF':>5} {'win%':>5} "
        f"{'<=-50%':>6} {'$':>9}")
    stats = {}
    for b, ps in by_book.items():
        s = book_stats([(p.pnl_pct or 0.0, p.pnl_usd or 0.0) for p in ps])
        stats[b] = s
        if not s["n"]:
            out(f"{b:9} {0:>4}   (no closed trades)")
            continue
        flag = "  (too few to judge)" if s["n"] < MIN_N else ""
        out(f"{b:9} {s['n']:>4} {_fmt(s['mean']):>7} [{_fmt(s['ci'][0]):>7},{_fmt(s['ci'][1]):>7}] "
            f"{_fmt(s['median']):>7} {_fmt(s['pf'], 2, False):>5} {s['win']:>5.0f} {s['big_loss']:>6.0f} "
            f"{s['usd']:>+9.2f}{flag}")

    # ---- vs the random book (the luck baseline) ----
    rnd = [p.pnl_pct or 0.0 for p in by_book.get("random", [])]
    pvals = {b: permutation_p([p.pnl_pct or 0.0 for p in ps], rnd)
             for b, ps in by_book.items() if b != "random" and ps}
    adj = holm(pvals)
    if rnd and pvals:
        out("\nvs random book (permutation test, Holm-corrected; p < 0.05 = unlikely to be luck):")
        for b in pvals:
            diff = (stats[b]["mean"] - statistics.fmean(rnd)) if stats[b]["n"] else float("nan")
            verdict = ("better than random" if adj[b] < 0.05 and diff > 0 else
                       "worse than random" if adj[b] < 0.05 else "no proven difference")
            out(f"  {b:9} diff {_fmt(diff)} pp · p {_fmt(pvals[b], 3, False)} · Holm {_fmt(adj[b], 3, False)} "
                f"· {verdict}")
    else:
        out("\n(no random-book trades yet: can't tell skill from luck)")

    # ---- per rule version ----
    out("\nby rule version (a change of rules starts a new version; compare like with like):")
    groups: dict[tuple, list] = {}
    for p in closed:
        groups.setdefault((p.book, p.rule_version or "(old)"), []).append(p.pnl_pct or 0.0)
    for (b, v), xs in sorted(groups.items()):
        out(f"  {b:9} {v:9} n {len(xs):>4} · mean {_fmt(statistics.fmean(xs))}% · "
            f"median {_fmt(statistics.median(xs))}%")

    # ---- shadow exits: same entries, other exit rules ----
    db = logger.db
    with logger.lock:
        sh = db.execute("SELECT pos_id, book, variant, pnl_pct FROM shadow WHERE closed_ts IS NOT NULL "
                        "AND opened_ts >= ?", (since,)).fetchall()
    actual = {p.id: p.pnl_pct or 0.0 for p in closed}
    if sh:
        out("\nshadow exits (same entries, different exit rule; 'actual' = what the book really did):")
        by: dict[tuple, list] = {}
        for pos_id, b, var, pnl in sh:
            by.setdefault((b, var), []).append((pos_id, pnl))
        for b in sorted({k[0] for k in by}):
            ids = set.intersection(*[{i for i, _ in by[k]} for k in by if k[0] == b]) & set(actual)
            if not ids:
                continue
            act = [actual[i] for i in ids]
            out(f"  {b} · {len(ids)} entries with every variant closed")
            out(f"    {'actual':16} mean {_fmt(statistics.fmean(act))}% · median {_fmt(statistics.median(act))}%")
            for var in VARIANTS:
                xs = [pnl for i, pnl in by.get((b, var), []) if i in ids]
                if xs:
                    out(f"    {var:16} mean {_fmt(statistics.fmean(xs))}% · median {_fmt(statistics.median(xs))}%")
    else:
        out("\nshadow exits: nothing closed yet")

    # ---- candidates: did we pick better than what we rejected? ----
    with logger.lock:
        rows = db.execute("SELECT book, decision, " + ", ".join(c for c, _ in HORIZONS) +
                          " FROM candidates WHERE ts >= ?", (since,)).fetchall()
    if rows:
        out("\ncandidates · mean forward return % (n) · accepted should beat rejected:")
        out(f"  {'book':9} {'decision':8} " + " ".join(f"{c[4:]:>13}" for c, _ in HORIZONS))
        agg: dict[tuple, list[list[float]]] = {}
        for r in rows:
            cols = agg.setdefault((r[0] or "(filter)", r[1]), [[] for _ in HORIZONS])
            for i, v in enumerate(r[2:]):
                if v is not None:
                    cols[i].append(v)
        for (b, dec), cols in sorted(agg.items()):
            cells = [f"{_fmt(statistics.fmean(c)):>7} ({len(c):>4})" if c else f"{'-':>13}" for c in cols]
            out(f"  {b:9} {dec:8} " + " ".join(cells))
    else:
        out("\ncandidates: none logged yet")
    out("\nRule of thumb: judge a book only after 100+ closed trades; one week of one version is not enough.")
    return {"stats": stats, "p": pvals, "holm": adj}
