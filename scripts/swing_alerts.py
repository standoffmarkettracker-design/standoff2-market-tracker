#!/usr/bin/env python3
"""
Swing-item alert bot.

Finds items whose price keeps dipping to a similar low and bouncing back to a
similar high (like the Arcane Fragment), then on every price update:

  * alerts when a watched item ENTERS its buy zone or sell zone,
  * ranks buy-zone items by urgency (how far the price has fallen away from
    its own recent trend, how deep it is under the buy line, the upside to the
    sell line and how reliable the item's swings have been),
  * announces items that newly fit the pattern, and items that stop fitting,
  * once a day posts the full ranked buy list.

Reads:   items.json, price_history_real_1.json, price_history_real_2.json
Writes:  swing_state.json  (what the bot already alerted on, so it never repeats)
         swing_data.json   (watchlist + zones + urgency, for the site)
Posts:   SWING_WEBHOOK if set, else DISCORD_WEBHOOK. Without either it prints only.

Flags:   --dry-run        print the Discord messages instead of posting
         --asof YYYY-MM-DD  pretend today is that date (replay / testing)
         --no-state        do not write swing_state.json / swing_data.json
Stdlib only, so the workflow needs no extra installs.
"""
import argparse, datetime as dt, json, math, os, statistics as st, sys, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- settings ---
FEE = 0.20                  # market keeps 20% of the sale price
WINDOW_DAYS = 548           # look back 18 months for the swing pattern
SWING_UP = 1.35             # a low counts once price rises 35% off it
SWING_DOWN = 1 / 1.30       # a high counts once price falls ~23% off it
BUY_PAD, SELL_PAD = 1.10, 0.90   # buy line = typical low +10%, sell line = typical high -10%

# to JOIN the watchlist
MIN_SWINGS, MIN_RATIO, MAX_CV = 5, 1.50, 0.25
MAX_SPREAD, MIN_CHANGE_DAYS, MIN_COVER = 20.0, 0.85, 0.95
MIN_LOW_GOLD, MIN_FLIPS, MIN_AVG_NET = 5.0, 2, 0.15
# to STAY on it (looser, so items don't flap on and off)
KEEP_SWINGS, KEEP_RATIO, KEEP_CV, KEEP_SPREAD, KEEP_AVG_NET = 4, 1.40, 0.32, 25.0, 0.08

REALERT_HOURS = 72          # same item + same zone is not re-alerted within this
ALERT_MIN_URGENCY = 50      # buy-zone entries under this score only show in the daily ranking
EXIT_PAD = 1.08             # leave the buy zone only above buy line x1.08 (sell zone: below sell line /1.08)
DIGEST_HOUR_UTC = 12        # the first run at/after this hour posts the daily ranking
FLOOR_BREAK = 0.75          # price under 75% of the typical low = pattern may be breaking

TIERS = [(70, "\U0001F534 BUY NOW"), (50, "\U0001F7E0 Strong"), (30, "\U0001F7E1 Watch"), (0, "⚪ Low")]


# ------------------------------------------------------------------ helpers ---
def load(name, default=None):
    p = ROOT / name
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    with open(ROOT / name, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def gold(v):
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.0f} G"
    if v >= 100:
        return f"{v:,.0f} G"
    return f"{v:,.2f}".rstrip("0").rstrip(".") + " G"


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def series_for(hist, name, start, end):
    h = hist.get(name) or {}
    out = []
    for k, v in h.items():
        try:
            d = dt.date.fromisoformat(k)
            p = float(v)
        except (TypeError, ValueError):
            continue
        if p > 0 and start <= d <= end:
            out.append((d, p))
    out.sort()
    return out


def smooth3(s):
    return [(s[i][0], st.median([p for _, p in s[max(0, i - 1):i + 2]])) for i in range(len(s))]


def zigzag(s):
    """Alternating lows (L) and highs (H) that moved at least SWING_UP / SWING_DOWN."""
    piv, mode = [], None
    lo = hi = s[0]
    for d, p in s:
        if mode is None:
            if p >= lo[1] * SWING_UP:
                piv.append(("L",) + lo); mode = "up"; hi = (d, p)
            elif p <= hi[1] * SWING_DOWN:
                piv.append(("H",) + hi); mode = "dn"; lo = (d, p)
            else:
                if p < lo[1]: lo = (d, p)
                if p > hi[1]: hi = (d, p)
        elif mode == "up":
            if p > hi[1]:
                hi = (d, p)
            elif p <= hi[1] * SWING_DOWN:
                piv.append(("H",) + hi); mode = "dn"; lo = (d, p)
        else:
            if p < lo[1]:
                lo = (d, p)
            elif p >= lo[1] * SWING_UP:
                piv.append(("L",) + lo); mode = "up"; hi = (d, p)
    return piv


def backtest(s, buy, sell):
    """Buy the first day price <= buy, sell the first day price >= sell. One at a time."""
    trades, held = [], None
    for d, p in s:
        if held is None and p <= buy:
            held = (d, p)
        elif held and p >= sell:
            trades.append(((p * (1 - FEE)) / held[1] - 1, (d - held[0]).days))
            held = None
    return trades


def analyse(name, hist, today, spread):
    s = series_for(hist, name, today - dt.timedelta(days=WINDOW_DAYS), today)
    if len(s) < 350:
        return None
    if st.median([p for _, p in s[-30:]]) < MIN_LOW_GOLD:
        return None
    piv = zigzag(smooth3(s))
    ups = [(a, b) for a, b in zip(piv, piv[1:]) if a[0] == "L" and b[0] == "H"]
    if len(ups) < KEEP_SWINGS:
        return None
    lows = [a[2] for a, _ in ups]
    highs = [b[2] for _, b in ups]
    lo, hi = st.median(lows), st.median(highs)
    ratio = st.median([b[2] / a[2] for a, b in ups])
    cv_lo = st.pstdev(lows) / st.mean(lows)
    cv_hi = st.pstdev(highs) / st.mean(highs)
    L = [p for p in piv if p[0] == "L"]
    gaps = [(b[1] - a[1]).days for a, b in zip(L, L[1:])]
    last90 = [x for x in s if (today - x[0]).days < 90]
    cover = len(last90) / 90
    changes = sum(1 for a, b in zip(last90, last90[1:]) if a[1] != b[1]) / max(1, len(last90) - 1)
    buy, sell = lo * BUY_PAD, hi * SELL_PAD
    trades = backtest(s, buy, sell)
    avg_net = st.mean([t[0] for t in trades]) if trades else None
    return {
        "item": name, "swings": len(ups), "typical_low": lo, "typical_high": hi, "ratio": ratio,
        "cv_low": cv_lo, "cv_high": cv_hi, "days_between_lows": st.median(gaps) if gaps else None,
        "cover90": cover, "change_days": changes, "spread": spread,
        "buy_below": buy, "sell_above": sell, "flips": len(trades), "avg_net": avg_net,
        "median_hold_days": st.median([t[1] for t in trades]) if trades else None,
        "series": s,
    }


def qualifies(a, keep=False):
    if a is None:
        return False
    sp = a["spread"] if a["spread"] is not None else 99
    if keep:
        return (a["swings"] >= KEEP_SWINGS and a["ratio"] >= KEEP_RATIO and a["cv_low"] <= KEEP_CV
                and a["cv_high"] <= KEEP_CV and sp <= KEEP_SPREAD and a["flips"] >= 1
                and (a["avg_net"] or -1) >= KEEP_AVG_NET)
    return (a["swings"] >= MIN_SWINGS and a["ratio"] >= MIN_RATIO and a["cv_low"] <= MAX_CV
            and a["cv_high"] <= MAX_CV and sp <= MAX_SPREAD and a["change_days"] >= MIN_CHANGE_DAYS
            and a["cover90"] >= MIN_COVER and a["typical_low"] >= MIN_LOW_GOLD
            and a["flips"] >= MIN_FLIPS and (a["avg_net"] or -1) >= MIN_AVG_NET)


def price_on_or_before(s, day):
    best = None
    for d, p in s:
        if d <= day:
            best = p
        else:
            break
    return best


def urgency(a, now, today):
    """0-100 buy urgency for an item in (or at) its buy zone, plus the reasons."""
    s = a["series"]
    recent = [p for d, p in s if 0 < (today - d).days <= 30]
    trend = st.median(recent) if len(recent) >= 5 else st.median([p for _, p in s[-30:]])
    mid = math.sqrt(a["typical_low"] * a["typical_high"])
    typical_dip = max(0.05, 1 - a["typical_low"] / mid)          # how far a normal dip goes below the middle
    below_trend = 1 - now / trend if trend else 0.0             # how far under its own 30-day trend
    depth = 1 - now / a["buy_below"]                            # how far under the buy line
    upside = (a["sell_above"] * (1 - FEE)) / now - 1            # net % if it gets back to the sell line
    p5 = price_on_or_before(s, today - dt.timedelta(days=5))
    drop5 = 1 - now / p5 if p5 else 0.0                         # how fast it just fell

    score = 0.0
    score += 35 * clamp(below_trend / (typical_dip * 1.5))
    score += 20 * clamp(depth / 0.15)
    score += 20 * clamp(upside / 0.60)
    score += 10 * clamp(a["flips"] / 6) + 5 * clamp((a["avg_net"] or 0) / 0.40)
    score += 10 * clamp(drop5 / 0.25)
    warn = None
    if now < a["typical_low"] * FLOOR_BREAK:
        score -= 25
        warn = f"under its usual floor ({gold(a['typical_low'])}), the pattern may be breaking"
    score = round(clamp(score, 0, 100))
    return {
        "score": score, "tier": next(t for cut, t in TIERS if score >= cut),
        "trend_30d": trend, "below_trend_pct": round(below_trend * 100, 1),
        "under_buy_pct": round(depth * 100, 1), "upside_net_pct": round(upside * 100, 1),
        "drop_5d_pct": round(drop5 * 100, 1), "warning": warn,
    }


def zone_of(a, now):
    if now <= a["buy_below"]:
        return "buy"
    if now >= a["sell_above"]:
        return "sell"
    return "mid"


# ----------------------------------------------------------------- discord ---
def post(lines, webhook, dry):
    """Send lines to Discord in <=1900-char messages."""
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > 1900:
            chunks.append(cur); cur = ""
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        if dry or not webhook:
            print("----- discord message -----\n" + c)
            continue
        req = urllib.request.Request(
            webhook, data=json.dumps({"content": c, "allowed_mentions": {"parse": []}}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "StandoffMarketTracker-SwingBot/1.0 (+https://standoff2markettracker.com)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"Discord: {r.status}")
        except urllib.error.HTTPError as e:
            print(f"Discord HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
            raise
    return len(chunks)


def buy_line(r):
    u = r["urgency"]
    ln = (f"{u['tier']} **{r['item']}** · {gold(r['price_now'])} (buy below {gold(r['buy_below'])}, sell above {gold(r['sell_above'])})"
          f"\n  urgency **{u['score']}** · {trend_words(u['below_trend_pct'])} · {u['upside_net_pct']:+.0f}% to the sell line after fee"
          f" · {r['flips']} past flips, avg {r['avg_net_pct']:+.0f}%")
    if u["warning"]:
        ln += f"\n  ⚠️ {u['warning']}"
    elif u["upside_net_pct"] < 5:
        ln += "\n  ⚠️ little left after the 20% fee at this price"
    return ln


def trend_words(pct):
    if abs(pct) < 1:
        return "right at its 30-day trend"
    return f"{abs(pct):.0f}% {'under' if pct > 0 else 'over'} its 30-day trend"


def sell_line(r):
    prem = r["price_now"] / r["sell_above"] - 1
    from_buy = r["price_now"] * (1 - FEE) / r["buy_below"] - 1
    return (f"\U0001F4B0 **{r['item']}** · {gold(r['price_now'])} ({prem * 100:+.0f}% over sell line {gold(r['sell_above'])})"
            f"\n  bought at the buy line = {from_buy * 100:+.0f}% after fee")


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--asof")
    ap.add_argument("--no-state", action="store_true")
    ap.add_argument("--state-file", default="swing_state.json")
    args = ap.parse_args()

    now_utc = dt.datetime.now(dt.timezone.utc)
    today = dt.date.fromisoformat(args.asof) if args.asof else now_utc.date()
    stamp = (dt.datetime.combine(today, dt.time(now_utc.hour, now_utc.minute), dt.timezone.utc)
             if args.asof else now_utc)

    hist = load("price_history_real_1.json", {}) or {}
    hist.update(load("price_history_real_2.json", {}) or {})
    items = {i["name"]: i for i in (load("items.json", []) or [])}
    state = load(args.state_file, None) or {}
    first_run = not state
    prev_watch = set(state.get("watch", []))
    zones = state.get("zones", {})
    last_alert = state.get("last_alert", {})

    # 1. who fits the pattern now
    watch, analysed = [], {}
    for name in hist:
        it = items.get(name, {})
        a = analyse(name, hist, today, it.get("spread"))
        if a is None:
            continue
        if qualifies(a) or (name in prev_watch and qualifies(a, keep=True)):
            watch.append(name); analysed[name] = a
    added = sorted(set(watch) - prev_watch)
    dropped = sorted(prev_watch - set(watch))

    # 2. where each watched item is now
    rows = []
    for name in watch:
        a = analysed[name]
        now = None
        if not args.asof:
            now = float(items.get(name, {}).get("price") or 0) or None
        if now is None:
            now = price_on_or_before(a["series"], today)
        if not now:
            continue
        z = zone_of(a, now)
        # hysteresis: an item only leaves a zone once it is clearly out of it,
        # so a price wobbling around the line doesn't re-alert every day
        before = zones.get(name)
        if before == "buy" and z == "mid" and now <= a["buy_below"] * EXIT_PAD:
            z = "buy"
        elif before == "sell" and z == "mid" and now >= a["sell_above"] / EXIT_PAD:
            z = "sell"
        r = {
            "item": name, "price_now": now, "zone": z,
            "buy_below": round(a["buy_below"], 2), "sell_above": round(a["sell_above"], 2),
            "typical_low": round(a["typical_low"], 2), "typical_high": round(a["typical_high"], 2),
            "swing_x": round(a["ratio"], 2), "swings_18mo": a["swings"], "flips": a["flips"],
            "avg_net_pct": round((a["avg_net"] or 0) * 100, 1),
            "median_hold_days": a["median_hold_days"], "days_between_lows": a["days_between_lows"],
            "type": items.get(name, {}).get("type", ""), "spread_pct": a["spread"],
        }
        r["urgency"] = urgency(a, now, today) if z == "buy" or now <= a["buy_below"] * 1.05 else None
        rows.append(r)

    # 3. zone changes worth an alert
    def recently(item, zone):
        t = last_alert.get(f"{item}|{zone}")
        return t and (stamp - dt.datetime.fromisoformat(t)).total_seconds() < REALERT_HOURS * 3600

    entered_buy, entered_sell = [], []
    for r in rows:
        before = zones.get(r["item"])
        if r["zone"] != before and r["zone"] in ("buy", "sell") and not first_run and not recently(r["item"], r["zone"]):
            (entered_buy if r["zone"] == "buy" else entered_sell).append(r)
            last_alert[f"{r['item']}|{r['zone']}"] = stamp.isoformat()
        zones[r["item"]] = r["zone"]
    for gone in dropped:
        zones.pop(gone, None)
    entered_buy.sort(key=lambda r: -r["urgency"]["score"])
    entered_sell.sort(key=lambda r: -(r["price_now"] / r["sell_above"]))

    in_buy = sorted([r for r in rows if r["zone"] == "buy"], key=lambda r: -r["urgency"]["score"])
    in_sell = sorted([r for r in rows if r["zone"] == "sell"], key=lambda r: -(r["price_now"] / r["sell_above"]))

    # 4. messages
    lines = []
    if first_run:
        lines.append(f"## \U0001F4E1 Swing bot is live: watching {len(watch)} items")
        lines.append("I'll post when one of them drops into its buy zone or climbs into its sell zone, and once a day with the full buy ranking.")
    loud = [r for r in entered_buy if r["urgency"]["score"] >= ALERT_MIN_URGENCY]
    if loud:
        lines.append(f"## \U0001F7E2 Entered the buy zone ({len(loud)})")
        lines += [buy_line(r) for r in loud]
    if entered_sell:
        lines.append(f"## \U0001F4B0 Entered the sell zone ({len(entered_sell)})")
        lines += [sell_line(r) for r in entered_sell]
    if added and not first_run:
        lines.append(f"## \U0001F195 New swing items ({len(added)})")
        for n in added:
            a = analysed[n]
            lines.append(f"**{n}** · swings {gold(a['typical_low'])} ↔ {gold(a['typical_high'])}, {a['swings']} times in 18 months,"
                         f" {a['flips']} test flips averaging {(a['avg_net'] or 0) * 100:+.0f}% after fee")
    if dropped and not first_run:
        lines.append(f"## \U0001F6AB No longer swinging ({len(dropped)})")
        lines.append(", ".join(f"**{n}**" for n in dropped))

    digest_due = (state.get("last_digest") != today.isoformat()) and (args.asof or now_utc.hour >= DIGEST_HOUR_UTC)
    if (digest_due or first_run) and (in_buy or in_sell):
        lines.append(f"## \U0001F4CB Daily ranking · {today.isoformat()}")
        if in_buy:
            lines.append(f"**In the buy zone now ({len(in_buy)}), most urgent first:**")
            top = [r for r in in_buy if r["urgency"]["score"] >= ALERT_MIN_URGENCY][:15]
            rest = [r for r in in_buy if r not in top]
            lines += [buy_line(r) for r in top]
            if rest:
                lines.append(f"Lower urgency ({len(rest)}): " + ", ".join(f"{r['item']} {gold(r['price_now'])} ({r['urgency']['score']})" for r in rest[:25]))
        if in_sell:
            lines.append(f"**In the sell zone now ({len(in_sell)}):** " + ", ".join(f"{r['item']} {gold(r['price_now'])}" for r in in_sell[:20]))
        state["last_digest"] = today.isoformat()

    if lines:
        lines.append("_Levels come from each item's last 18 months; past swings don't guarantee the next one._")
        webhook = os.environ.get("SWING_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK", "")
        n = post(lines, webhook, args.dry_run)
        print(f"Posted {n} message(s).")
    else:
        print("Nothing new to alert.")

    print(f"watching {len(watch)} | buy zone {len(in_buy)} | sell zone {len(in_sell)} | "
          f"entered buy {len(entered_buy)} (alerted {len([r for r in entered_buy if r['urgency']['score'] >= ALERT_MIN_URGENCY])}) | entered sell {len(entered_sell)} | new {len(added)} | dropped {len(dropped)}")

    # 5. remember
    if not args.no_state:
        state.update({"updated": stamp.isoformat(), "watch": sorted(watch), "zones": zones,
                      "last_alert": {k: v for k, v in last_alert.items()
                                     if (stamp - dt.datetime.fromisoformat(v)).days < 30}})
        save(args.state_file, state)
        save("swing_data.json", {"generated": stamp.isoformat(), "fee": FEE, "items": rows})


if __name__ == "__main__":
    main()
