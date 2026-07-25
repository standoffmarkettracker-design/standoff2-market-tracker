#!/usr/bin/env python3
"""
Standoff 2 Market Tracker - Upgraded Price Update Script
- Scrapes standoff-2.com/shop (existing DataTables method, reliable)
- Also scrapes standoff-2.com/en/market to discover new collections/items
- Auto-adds newly discovered items to items.json and price_history files
- Respects GIVEAWAY_PINNED items (prices never overwritten)
- Never writes zero or negative prices
"""

import json, re, sys, datetime
from pathlib import Path

ROOT     = Path(__file__).parent.parent
SHOP_URL = "https://standoff-2.com/shop/"
MKT_URL  = "https://standoff-2.com/en/market"

GIVEAWAY_PINNED = {
    "weekly":   'Sticker "Province"',
    "biweekly": 'AKR "Scylla" StatTrack',
    "monthly":  'S2 Mantis "Ink Wash"',
}

def parse_num(val):
    if val is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(val).strip())
    if not s:
        return None
    ld, lc = s.rfind("."), s.rfind(",")
    if ld > lc:
        s = s.replace(",", "")
    elif lc > ld:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None

def strip_html(text):
    t = re.sub(r"<[^>]+>", "", str(text))
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r" {2,}", " ", t).strip()
    return t

def is_valid_name(name: str) -> bool:
    if not name or len(name) < 3 or len(name) > 120:
        return False
    if "\n" in name or "\r" in name or "\t" in name:
        return False
    if name.count("  ") > 2:
        return False
    # Reject names with an odd number of double-quote characters -- this is
    # the signature of a mangled apostrophe (e.g. Horseman's -> Horseman"s)
    # picked up by DOM/text scraping, which otherwise creates a corrupted
    # near-duplicate of an existing item.
    if name.count('"') % 2 != 0:
        return False
    return True

def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

def fetch_shop_catalog():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", viewport={"width": 1280, "height": 800})
        page = context.new_page()
        print("  [shop] Loading shop page...")
        try:
            page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  [shop] goto warning: {e}")
        page.wait_for_timeout(4000)
        print("  [shop] Firing DataTables AJAX...")
        result = page.evaluate("""async () => {
            const nonceInput = document.querySelector('[name*="wdtNonce"]');
            const nonce = nonceInput ? nonceInput.value : "";
            if (!nonce) return {error: "No nonce found"};
            const body = new URLSearchParams({
                draw: "1",
                "columns[0][data]": "0", "columns[0][name]": "Name",
                "columns[1][data]": "1", "columns[1][name]": "end_price",
                "columns[2][data]": "2", "columns[2][name]": "delta_D",
                "columns[3][data]": "3", "columns[3][name]": "delta_W",
                "columns[4][data]": "4", "columns[4][name]": "delta_M",
                "columns[5][data]": "5", "columns[5][name]": "delta_Y",
                "columns[6][data]": "6", "columns[6][name]": "spread",
                "columns[7][data]": "7", "columns[7][name]": "volatility",
                "order[0][column]": "1", "order[0][dir]": "desc",
                start: "0", length: "3000",
                "search[value]": "", "search[regex]": "false",
                wdtNonce: nonce
            });
            try {
                const resp = await fetch("/wp-admin/admin-ajax.php?action=get_wdtable&table_id=4", {
                    method: "POST", credentials: "same-origin",
                    headers: {"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
                    body: body.toString()
                });
                const text = await resp.text();
                return {nonce, status: resp.status, len: text.length, data: text};
            } catch(e) { return {error: e.toString(), nonce}; }
        }""")
        browser.close()
    print(f"  [shop] status={result.get('status')}  len={result.get('len', 0)}")
    if "error" in result:
        raise RuntimeError(f"Browser fetch error: {result['error']}")
    raw = result.get("data", "")
    if not raw or not raw.strip():
        raise RuntimeError("Empty response from in-browser AJAX call")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"JSON parse error: {e} -- preview: {raw[:200]}")
    rows = data.get("data", [])
    print(f"  [shop] rows received: {len(rows)}")
    if len(rows) < 10:
        raise RuntimeError(f"Too few rows: {len(rows)}")
    catalog = {}
    for row in rows:
        name  = strip_html(row[0])
        price = parse_num(row[1])
        if name and price and price > 0:
            catalog[name] = {"price": price, "day": parse_num(row[2]), "week": parse_num(row[3]), "month": parse_num(row[4]), "year": parse_num(row[5]), "spread": parse_num(row[6]), "vol": parse_num(row[7])}
    print(f"  [shop] valid catalog entries: {len(catalog)}")
    return catalog

def fetch_market_discovery():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", viewport={"width": 1280, "height": 800})
        page = context.new_page()
        print("  [market] Loading market page...")
        try:
            page.goto(MKT_URL, wait_until="networkidle", timeout=90000)
        except Exception as e:
            print(f"  [market] goto warning: {e}")
        page.wait_for_timeout(5000)
        print("  [market] Extracting items from DOM and API...")
        result = page.evaluate("""async () => {
            const items = [];
            const cards = document.querySelectorAll('[class*="item"],[class*="card"],[class*="lot"],[class*="skin"],[class*="product"]');
            cards.forEach(card => {
                const nameEl = card.querySelector('[class*="name"],[class*="title"],h3,h4');
                const priceEl = card.querySelector('[class*="price"],[class*="cost"],[class*="gold"]');
                const collEl = card.querySelector('[class*="collection"],[class*="category"]');
                if (nameEl && priceEl) {
                    items.push({name: nameEl.textContent.trim(), price: priceEl.textContent.trim(), collection: collEl ? collEl.textContent.trim() : "", source: "dom"});
                }
            });
            const scripts = document.querySelectorAll('script:not([src])');
            const jsonItems = [];
            scripts.forEach(s => {
                const text = s.textContent;
                const matches = text.match(/"name":\s*"([^"]+)".{0,200}"price":\s*(\d+)/g);
                if (matches) {
                    matches.forEach(m => {
                        const nm = m.match(/"name":\s*"([^"]+)"/);
                        const pr = m.match(/"price":\s*(\d+)/);
                        if (nm && pr) jsonItems.push({name: nm[1], price: parseInt(pr[1]), source: "script"});
                    });
                }
            });
            const apiItems = [];
            const endpoints = ['/api/market/items','/api/items','/en/api/market','/api/v1/market/items','/api/market/lots'];
            for (const ep of endpoints) {
                try {
                    const r = await fetch(ep, {credentials: "same-origin"});
                    if (r.ok) { const data = await r.json(); apiItems.push({endpoint: ep, sample: JSON.stringify(data).slice(0,300)}); }
                } catch(e) {}
            }
            return {domItems: items.slice(0,500), jsonItems: jsonItems.slice(0,500), apiItems};
        }""")
        browser.close()
    dom_items = result.get("domItems", [])
    json_items = result.get("jsonItems", [])
    api_items = result.get("apiItems", [])
    print(f"  [market] DOM={len(dom_items)} Script={len(json_items)} API hits={len(api_items)}")
    if api_items:
        for a in api_items: print(f"    {a['endpoint']}: {a['sample'][:100]}")
    all_found = {}
    for item in dom_items + json_items:
        name = item.get("name","").strip()
        price = parse_num(item.get("price",0))
        if is_valid_name(name) and price and 0 < price <= 10_000_000:
            all_found[name] = {"price": price, "collection": item.get("collection","").strip(), "source": item.get("source","dom")}
    print(f"  [market] unique valid items: {len(all_found)}")
    return all_found

def apply_updates(shop_catalog, market_discovered, today):
    items    = load_json(ROOT / "items.json")
    hist1    = load_json(ROOT / "price_history_real_1.json")
    hist2    = load_json(ROOT / "price_history_real_2.json")
    coll_map = load_json(ROOT / "collections.json")
    # Sanitize: remove corrupted entries (newlines in name or impossible prices)
    items = [i for i in items if "\n" not in i["name"] and 0 < i.get("price", 0) <= 10_000_000]
    # Safety net: dedupe by exact name (keep first occurrence) in case
    # duplicate entries ever slip into items.json from any source.
    seen_names, deduped, dupes_removed = set(), [], 0
    for i in items:
        if i["name"] in seen_names:
            dupes_removed += 1
            continue
        seen_names.add(i["name"])
        deduped.append(i)
    if dupes_removed:
        print(f"  [dedupe] Removed {dupes_removed} duplicate item(s) from items.json")
    items = deduped
    purge_zero_dates(hist1, hist2)
    pinned_names   = set(GIVEAWAY_PINNED.values())
    existing_names = {item["name"] for item in items}
    updated = skipped_pinned = skipped_zero = new_items = 0
    for item in items:
        name = item["name"]
        if name in pinned_names:
            skipped_pinned += 1
            continue
        if name not in shop_catalog:
            continue
        rec = shop_catalog[name]
        if not rec["price"] or rec["price"] <= 0:
            skipped_zero += 1
            continue
        for k in ["price","day","week","month","year","spread","vol"]:
            if rec[k] is not None:
                item[k] = rec[k]
        if name in hist1:
            hist1[name][today] = rec["price"]
        elif name in hist2:
            hist2[name][today] = rec["price"]
        updated += 1
    all_new = {}
    for name, rec in shop_catalog.items():
        if name not in existing_names and name not in pinned_names:
            all_new[name] = {"price": rec["price"], "day": rec.get("day"), "week": rec.get("week"), "month": rec.get("month"), "year": rec.get("year"), "spread": rec.get("spread"), "vol": rec.get("vol"), "collection": coll_map.get(name,""), "source": "shop"}
    for name, rec in market_discovered.items():
        if name not in existing_names and name not in pinned_names and name not in all_new:
            all_new[name] = {"price": rec["price"], "day": None, "week": None, "month": None, "year": None, "spread": None, "vol": None, "collection": rec.get("collection", coll_map.get(name,"")), "source": "market"}
    for name, rec in all_new.items():
        if not is_valid_name(name) or not rec["price"] or rec["price"] <= 0 or rec["price"] > 10_000_000:
            continue
        items.append({"name": name, "price": rec["price"], "day": rec["day"] or 0, "week": rec["week"] or 0, "month": rec["month"] or 0, "year": rec["year"] or 0, "spread": rec["spread"] or 0, "vol": rec["vol"] or 0, "type": "", "rarity": ""})
        target_hist = hist1 if len(hist1) < 1200 else hist2
        target_hist[name] = {today: rec["price"]}
        if rec.get("collection"): coll_map[name] = rec["collection"]
        new_items += 1
        print(f"  [new] Auto-added: {name}  price={rec['price']}  source={rec['source']}")
    save_json(ROOT / "items.json", items)
    save_json(ROOT / "price_history_real_1.json", hist1)
    save_json(ROOT / "price_history_real_2.json", hist2)
    save_json(ROOT / "collections.json", coll_map)
    print(f"\n  Summary: updated={updated} new={new_items} zero_skipped={skipped_zero} pinned={skipped_pinned} total={len(items)}")
    return updated, new_items


def purge_zero_dates(hist1, hist2):
    """Remove any date entries where price is 0 or null (scraper glitch dates)."""
    purged = 0
    for history in list(hist1.values()) + list(hist2.values()):
        bad_dates = [d for d, p in history.items() if not p or p <= 0]
        for d in bad_dates:
            del history[d]
            purged += 1
    if purged:
        print(f"  [purge] Removed {purged} zero-price history entries")
    return purged

# ─── Flip Finder precompute ───────────────────────────────────────────────────
# Computes per-item flip analytics once per price update so the site never has
# to download or crunch the full history client-side. Output: flip_data.json

FLIP_DIP_PCT = 20      # dip definition for the recovery backtest (matches 20% market fee)
FLIP_WINDOW = 30       # days allowed for a dip to recover
FLIP_SCORE_FEE = 20    # official marketplace commission used in the score

def _flip_slope_pct(prices, k):
    n = len(prices); m = min(k, n)
    if m < 3: return 0.0
    seg = prices[n-m:]
    sx = sy = sxy = sxx = 0.0
    for i, y in enumerate(seg):
        sx += i; sy += y; sxy += i*y; sxx += i*i
    denom = m*sxx - sx*sx
    if not denom: return 0.0
    slope = (m*sxy - sx*sy) / denom
    mean = sy / m
    return (slope/mean)*100 if mean else 0.0

def _flip_backtest(prices, pre):
    n = len(prices); events = recovered = 0; days = []
    i = 30
    while i < n:
        tavg = (pre[i]-pre[i-30])/30
        if tavg > 0 and prices[i] <= tavg*(1-FLIP_DIP_PCT/100):
            events += 1
            for j in range(i+1, min(i+FLIP_WINDOW+1, n)):
                if prices[j] >= tavg:
                    recovered += 1; days.append(j-i); break
            i += 7
        i += 1
    days.sort()
    med = days[len(days)//2] if days else -1
    return events, recovered, med

def _flip_round(x, nd=2):
    return round(float(x), nd)

def build_flip_data(today):
    """Step 4: precompute flip analytics into flip_data.json."""
    items = load_json(ROOT / "items.json")
    hist = load_json(ROOT / "price_history_real_1.json")
    hist.update(load_json(ROOT / "price_history_real_2.json"))
    out = {}
    for it in items:
        name = it.get("name"); price = it.get("price") or 0
        h = hist.get(name)
        if not h or price <= 0: continue
        dates = sorted(h.keys())
        prices = [h[d] for d in dates]
        n = len(prices)
        if n < 35: continue
        pre = [0.0]
        for p in prices: pre.append(pre[-1]+p)
        latest = prices[-1]
        def avg(k):
            m = min(k, n); return (pre[n]-pre[n-m])/m
        avg7, avg30, avg90 = avg(7), avg(30), avg(90)
        seg30 = prices[-30:] if n >= 30 else prices
        mn30, mx30 = min(seg30), max(seg30)
        mean30 = avg30
        var = sum((p-mean30)**2 for p in seg30)/(len(seg30)-1) if len(seg30) > 1 else 0.0
        stdev30 = var ** 0.5
        vol_pct = (stdev30/mean30)*100 if mean30 else 0.0
        z = (latest-mean30)/stdev30 if stdev30 else 0.0
        range_pct = (latest-mn30)/(mx30-mn30) if mx30 > mn30 else 0.5
        slope14 = _flip_slope_pct(prices, 14)
        discount = (avg30-price)/avg30*100 if avg30 else 0.0
        events, recovered, med = _flip_backtest(prices, pre)
        rate = recovered/events if events else None
        thin = 1 if ((it.get("spread") or 0) > 30 or (it.get("vol") or 0) > 40) else 0
        s_disc = max(0.0, min(1.0, (discount-FLIP_SCORE_FEE)/20)) * 25
        ev_w = min(events/3, 1)
        s_rec = (0.35 if rate is None else rate) * ev_w * 30
        s_trend = max(0.0, min(1.0, (slope14+1)/1)) * 15
        s_liq = 5 if thin else 15
        s_range = (1 - max(0.0, min(1.0, range_pct))) * 15
        score = round(s_disc + s_rec + s_trend + s_liq + s_range)
        # 30-point spark downsampled from last 90 days
        seg90 = prices[-90:]
        step = max(1, len(seg90)//30)
        spark = [ _flip_round(seg90[i], 4 if seg90[i] < 100 else 1) for i in range(0, len(seg90), step) ][:30]
        out[name] = [
            _flip_round(avg7), _flip_round(avg30), _flip_round(avg90),
            _flip_round(mn30), _flip_round(mx30), _flip_round(vol_pct),
            _flip_round(z), _flip_round(range_pct, 3), _flip_round(slope14),
            _flip_round(discount, 1), events, recovered,
            (-1 if rate is None else _flip_round(rate, 3)), med, thin, score, n, spark
        ]
    data = {
        "generated": today, "dipPct": FLIP_DIP_PCT, "windowDays": FLIP_WINDOW,
        "scoreFee": FLIP_SCORE_FEE,
        "legend": ["avg7","avg30","avg90","min30","max30","volPct","z","rangePct",
                   "slope14","discount","events","recovered","rate","medianDays",
                   "thin","score","n","spark"],
        "items": out
    }
    save_json(ROOT / "flip_data.json", data)
    print(f"  [flip] Wrote flip_data.json for {len(out)} items")
    return len(out)


def main():
    today = datetime.date.today().isoformat()
    print(f"=== Standoff 2 Price Update - {today} ===\n")
    print("Step 1: Fetching shop catalog...")
    try:
        shop_catalog = fetch_shop_catalog()
    except Exception as e:
        print(f"ERROR in shop fetch: {e}", file=sys.stderr)
        sys.exit(1)
    if len(shop_catalog) < 100:
        print(f"ERROR: only {len(shop_catalog)} shop entries -- aborting", file=sys.stderr)
        sys.exit(1)
    print("\nStep 2: Discovering new items from market...")
    try:
        market_discovered = fetch_market_discovery()
    except Exception as e:
        print(f"WARNING: market discovery failed ({e}) -- continuing with shop only")
        market_discovered = {}
    print("\nStep 3: Applying updates...")
    updated, new_items = apply_updates(shop_catalog, market_discovered, today)
    if updated == 0 and new_items == 0:
        print("WARNING: no items updated or added", file=sys.stderr)
        sys.exit(1)
    print("\nStep 4: Building flip analytics...")
    try:
        from flip_engine import build_flip_data as build_flip_v2
        build_flip_v2(ROOT, today)
    except Exception as e:
        print(f"WARNING: flip precompute failed ({e}) -- site falls back gracefully")
    print(f"\nDone. {updated} updated, {new_items} new items added -- {today}")

if __name__ == "__main__":
    main()
