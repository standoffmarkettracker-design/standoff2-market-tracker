"""Flip Finder engine v2  —  evidence-based rebuild.

Replaces build_flip_data() in scripts/update_prices.py.

WHY THIS EXISTS
---------------
v1's score barely discriminated. Measured on the live 2,255-item feed:
  * only 1.1% of items cleared the 20% discount cliff, so the single most
    predictive input contributed nothing for 99% of the catalogue
  * 61% of items got the full trend points and 96% got the full liquidity
    points, so both were near-constants
  * result: scores clustered 45-55, mean 48 - a ranking that barely ranked

WHAT THE BACKTEST SAYS (2,072 items, >=180d history, 20% fee)
-------------------------------------------------------------
  * Breakeven needs +25% gross. Dip depth is the dominant predictor of
    reaching it: 10% dip -> 43% hit @30d;  25% -> 72%;  40% -> 78%.
  * An item's own past recovery rate is genuinely predictive but modest
    (r=0.28 across a train/test split). Ranking by it lifts the realised
    hit rate from a 48.4% baseline to 56.5% for the top quintile, and the
    bottom quintile drops to 39.9%. Worth using, worth shrinking hard.
  * Downside is the number nobody was showing: 1 flip in 10 goes 21-35%
    underwater before it recovers, if it recovers at all.

So: weight dip depth heavily, shrink per-item history toward a prior,
and always surface the drawdown.
"""
import json, statistics as st
from pathlib import Path

# ─── tunables ────────────────────────────────────────────────────────────────
FEE_PCT       = 20      # marketplace commission
MARGINS       = (0.05, 0.15, 0.25)   # profit tiers wanted AFTER fees
MARGIN        = 0.05    # default tier used for the headline score
HORIZON       = 30      # days allowed to reach target
BASE_K        = 30      # trailing window for the baseline
MIN_HISTORY   = 60      # days of history required to analyse an item
SHRINK_W      = 8.0     # pseudo-observations pulled toward the prior
DIP_BUCKETS   = (10, 15, 20, 25, 30, 40)

BE = 1.0 / (1.0 - FEE_PCT / 100.0)   # 1.25x gross to break even

# Global priors measured from the full-catalogue backtest (hit rate @30d,
# target = breakeven +5%). Used to shrink thin per-item samples.
PRIOR_BY_DIP = {10: 0.433, 15: 0.531, 20: 0.639, 25: 0.724, 30: 0.756, 40: 0.780}


def _prior_for(dip):
    """Interpolate the measured prior for an arbitrary dip depth."""
    ks = sorted(PRIOR_BY_DIP)
    if dip <= ks[0]:
        return PRIOR_BY_DIP[ks[0]] * max(0.0, dip / ks[0])
    if dip >= ks[-1]:
        return PRIOR_BY_DIP[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= dip <= b:
            t = (dip - a) / (b - a)
            return PRIOR_BY_DIP[a] + t * (PRIOR_BY_DIP[b] - PRIOR_BY_DIP[a])
    return 0.5


def _median(xs):
    return st.median(xs) if xs else None


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[i]


def _backtest(prices, dip_pct, horizon=HORIZON, k=BASE_K, margin=MARGIN):
    """Replay this item's own history: when it sat dip_pct under its trailing
    median, did it reach a price that nets `margin` profit after fees?"""
    n = len(prices)
    trials = wins = 0
    days, maes = [], []
    i = k
    while i < n - horizon:
        base = st.median(prices[i - k:i])
        buy = prices[i]
        if not base or buy <= 0 or buy > base * (1 - dip_pct / 100.0):
            i += 1
            continue
        fut = prices[i + 1:i + 1 + horizon]
        if not fut:
            break
        trials += 1
        target = buy * BE * (1 + margin)
        hit = next((j + 1 for j, p in enumerate(fut) if p >= target), None)
        maes.append((min(fut) - buy) / buy * 100.0)
        if hit:
            wins += 1
            days.append(hit)
        i += max(horizon // 2, 3)      # don't count overlapping entries
    return trials, wins, days, maes


def _slope_pct_per_day(prices, k=14):
    n = len(prices)
    m = min(k, n)
    if m < 3:
        return 0.0
    seg = prices[n - m:]
    sx = sy = sxy = sxx = 0.0
    for i, y in enumerate(seg):
        sx += i; sy += y; sxy += i * y; sxx += i * i
    den = m * sxx - sx * sx
    if not den:
        return 0.0
    slope = (m * sxy - sx * sy) / den
    mean = sy / m
    return (slope / mean) * 100.0 if mean else 0.0


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def analyse_item(item, prices):
    """Return the analytics row for one item, or None if not analysable."""
    price = item.get("price") or 0
    n = len(prices)
    if price <= 0 or n < MIN_HISTORY:
        return None

    seg = prices[-BASE_K:]
    baseline = st.median(seg)                 # robust vs the mean v1 used
    if not baseline or baseline <= 0:
        return None

    dip = (baseline - price) / baseline * 100.0

    # ---- fee arithmetic ----
    breakeven = price * BE                       # sell here just to get gold back
    targets = {int(m * 100): _r(breakeven * (1 + m)) for m in MARGINS}

    # How much room is there between today's price and the reversion baseline?
    headroom = (baseline / breakeven - 1) * 100.0 if breakeven else 0.0

    # ---- per-item backtest, one hit rate per profit tier ----
    # Anchor the bucket to the item's ACTUAL dip so a barely-dipped item is not
    # credited with the recovery odds of a deeply-dipped one.
    bucket = min(DIP_BUCKETS, key=lambda b: abs(b - max(dip, 0)))
    tiers = {}
    trials = 0
    maes = []
    for m in MARGINS:
        tr, wins, days, mae = _backtest(prices, bucket, margin=m)
        trials = max(trials, tr)
        if mae and not maes:
            maes = mae
        prior_m = _prior_for(max(dip, 0)) * (1.0 - 0.9 * (m - 0.05))
        rate = (wins + SHRINK_W * prior_m) / (tr + SHRINK_W) if (tr or SHRINK_W) else prior_m
        # an item that is not actually dipped does not get dip-recovery odds
        if dip < 5:
            rate *= max(0.0, dip / 5.0)
        tiers[int(m * 100)] = {
            "rate": _r(rate, 3),
            "raw": _r((wins / tr) if tr else None, 3),
            "trials": tr,
            "days": _median(days),
        }

    hit_rate = tiers[5]["rate"] or 0.0
    raw_rate = tiers[5]["raw"]
    med_days = tiers[5]["days"]
    med_mae = _median(maes)
    p10_mae = _pct(maes, 0.10)

    # ---- v1 descriptive stats (kept so the existing panel keeps working) ----
    def _avg(k):
        seg_k = prices[-k:] if n >= k else prices
        return sum(seg_k) / len(seg_k)
    avg7, avg30, avg90 = _avg(7), _avg(30), _avg(90)
    seg30 = prices[-30:] if n >= 30 else prices
    mn30, mx30 = min(seg30), max(seg30)
    if len(seg30) > 1:
        mean30 = sum(seg30) / len(seg30)
        stdev30 = (sum((x - mean30) ** 2 for x in seg30) / (len(seg30) - 1)) ** 0.5
    else:
        mean30, stdev30 = seg30[0], 0.0
    vol_pct = (stdev30 / mean30 * 100.0) if mean30 else 0.0
    zscore = ((price - mean30) / stdev30) if stdev30 else 0.0
    range_pct = ((price - mn30) / (mx30 - mn30)) if mx30 > mn30 else 0.5

    # ---- trend, liquidity, confidence ----
    slope14 = _slope_pct_per_day(prices, 14)
    spread = item.get("spread") or 0
    vol = item.get("vol") or 0

    liq = 1.0
    if spread > 20: liq -= min((spread - 20) / 60.0, 0.5)
    if vol > 30:    liq -= min((vol - 30) / 70.0, 0.4)
    liq = max(0.05, min(1.0, liq))

    conf = min(1.0, trials / 6.0) * 0.7 + min(1.0, n / 365.0) * 0.3
    thin = 1 if (spread > 30 or vol > 40) else 0
    recovered = int(round((tiers[5]["raw"] or 0) * trials)) if trials else 0

    # ---- expected value, in gold, per unit bought ----
    profit_if_hit = price * MARGIN                    # by construction of the target
    downside = abs(p10_mae or 25.0) / 100.0 * price   # bad-case paper drawdown
    ev_gold = hit_rate * profit_if_hit - (1 - hit_rate) * downside * 0.35
    ev_pct = (ev_gold / price * 100.0) if price else 0.0

    # ---- score 0..100, continuous and EV-led ----
    s_ev   = max(0.0, min(1.0, ev_pct / 5.0)) * 38     # expected value is the point
    s_dip  = max(0.0, min(1.0, dip / 30.0)) * 24       # depth = strongest measured signal
    s_hit  = max(0.0, min(1.0, (hit_rate - 0.30) / 0.45)) * 18
    s_liq  = liq * 10
    s_conf = conf * 10
    pen = min(12.0, abs(slope14 + 1.0) * 6.0) if slope14 < -1.0 else 0.0
    score = max(0, min(100, round(s_ev + s_dip + s_hit + s_liq + s_conf - pen)))

    # Tier: an honest three-way split rather than a fake-precise number.
    #   2 = worth a look   1 = marginal   0 = not worth flipping right now
    if dip >= 12 and hit_rate >= 0.50 and liq >= 0.5 and ev_pct > 0:
        tier = 2
    elif dip >= 6 and hit_rate >= 0.35 and ev_pct > -1:
        tier = 1
    else:
        tier = 0

    # ---- risk label ----
    dd = abs(p10_mae or 0)
    if dd >= 30 or liq < 0.4:
        risk = 3
    elif dd >= 18:
        risk = 2
    else:
        risk = 1

    # ---- sparkline: last 90 days, EVENLY sampled, always ending on today ----
    seg90 = prices[-90:]
    if len(seg90) <= 30:
        spark = seg90[:]
    else:
        step = (len(seg90) - 1) / 29.0
        spark = [seg90[int(round(i * step))] for i in range(30)]
    spark = [_r(p, 4 if p < 100 else 1) for p in spark]

    return {
        "price":     _r(price),
        "avg7":      _r(avg7),
        "avg30":     _r(avg30),
        "avg90":     _r(avg90),
        "min30":     _r(mn30),
        "max30":     _r(mx30),
        "volPct":    _r(vol_pct),
        "z":         _r(zscore),
        "rangePct":  _r(range_pct, 3),
        "thin":      thin,
        "recovered": recovered,
        "baseline":  _r(baseline),
        "dip":       _r(dip, 1),
        "breakeven": _r(breakeven),
        "tier":      tier,
        "targets":   targets,
        "tiers":     tiers,
        "headroom":  _r(headroom, 1),
        "hitRate":   _r(hit_rate, 3),
        "rawRate":   _r(raw_rate, 3),
        "trials":    trials,
        "bucket":    bucket,
        "medDays":   med_days,
        "medMae":    _r(med_mae, 1),
        "p10Mae":    _r(p10_mae, 1),
        "profitHit": _r(profit_if_hit),
        "evGold":    _r(ev_gold),
        "evPct":     _r(ev_pct, 2),
        "liq":       _r(liq, 2),
        "conf":      _r(conf, 2),
        "slope14":   _r(slope14, 2),
        "risk":      risk,
        "score":     score,
        "n":         n,
        "spark":     spark,
    }


def build_flip_data(root, today):
    root = Path(root)
    items = json.load(open(root / "items.json"))
    hist = json.load(open(root / "price_history_real_1.json"))
    hist.update(json.load(open(root / "price_history_real_2.json")))

    out = {}
    for it in items:
        name = it.get("name")
        h = hist.get(name)
        if not h:
            continue
        prices = [h[d] for d in sorted(h)]
        prices = [p for p in prices if p and p > 0]
        row = analyse_item(it, prices)
        if row:
            out[name] = row

    # ── emit in the v1 array shape, with new fields appended ────────────
    # The live FlipFinderPanel does `fd.legend.forEach(...)` and indexes rows
    # as arrays. Keeping that shape means this engine can ship on its own
    # without breaking the tab; the new keys sit at the end, ignored until
    # the new panel reads them.
    LEGEND = [
        # --- v1 keys, still read by the current panel ---
        "avg7", "avg30", "avg90", "min30", "max30", "volPct", "z", "rangePct",
        "slope14", "discount", "events", "recovered", "rate", "medianDays",
        "thin", "score", "n", "spark",
        # --- v2 additions ---
        "dip", "baseline", "breakeven", "tier", "hitRate", "trials", "p10Mae",
        "medMae", "evGold", "evPct", "risk", "liq", "conf", "profitHit",
        "t5", "t15", "t25", "d5", "d15", "d25", "g5", "g15", "g25",
    ]

    rows = {}
    for name, r in out.items():
        stub = r["tier"] == 0 and r["score"] < 25
        t = r.get("tiers") or {}
        def tv(m, k, d=None):
            e = t.get(m) or t.get(str(m)) or {}
            return e.get(k, d)
        rows[name] = [
            r["avg7"], r["avg30"], r["avg90"], r["min30"], r["max30"],
            r["volPct"], r["z"], r["rangePct"], r["slope14"], r["dip"],
            r["trials"], r["recovered"], r["hitRate"], r["medDays"],
            r["thin"], r["score"], r["n"],
            [] if stub else r["spark"],
            r["dip"], r["baseline"], r["breakeven"], r["tier"], r["hitRate"],
            r["trials"], r["p10Mae"], r["medMae"], r["evGold"], r["evPct"],
            r["risk"], r["liq"], r["conf"], r["profitHit"],
            tv(5, "rate"), tv(15, "rate"), tv(25, "rate"),
            tv(5, "days"), tv(15, "days"), tv(25, "days"),
            (r.get("targets") or {}).get(5), (r.get("targets") or {}).get(15),
            (r.get("targets") or {}).get(25),
        ]

    data = {
        "v": 2,
        "generated": today,
        "fee": FEE_PCT,
        "scoreFee": FEE_PCT,          # v1 alias, still read by the current panel
        "dipPct": DIP_BUCKETS[2],     # v1 alias
        "windowDays": HORIZON,        # v1 alias
        "margins": list(MARGINS),
        "horizon": HORIZON,
        "priors": PRIOR_BY_DIP,
        "legend": LEGEND,
        "items": rows,
    }
    json.dump(data, open(root / "flip_data.json", "w"), separators=(",", ":"))

    t2 = sum(1 for r in out.values() if r["tier"] == 2)
    t1 = sum(1 for r in out.values() if r["tier"] == 1)
    print(f"  [flip] v2: {len(out)} analysed | {t2} worth a look, {t1} marginal, "
          f"{len(out)-t2-t1} not worth flipping (fee {FEE_PCT}%)")
    return len(out)


if __name__ == "__main__":
    import sys, datetime
    build_flip_data(sys.argv[1] if len(sys.argv) > 1 else ".",
                    datetime.date.today().isoformat())
