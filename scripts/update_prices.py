#!/usr/bin/env python3
"""
Standoff 2 Market Tracker - Daily Price Update Script (Playwright version)
Loads standoff-2.com/shop in a real browser, extracts the session nonce,
then fires the DataTables AJAX call FROM INSIDE the browser (with cookies)
to get all 3000+ items in one request.
Run: python scripts/update_prices.py
"""

import json, re, sys, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent

SHOP_URL = "https://standoff-2.com/shop/"

GIVEAWAY_PINNED = {
    "weekly": 'Sticker "Province"',
    "biweekly": 'AKR "Scylla" StatTrack',
    "monthly": 'S2 Mantis "Ink Wash"',
}


def parse_num(val):
    if val is None:
        return None
    s = str(val).strip()
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    last_dot = s.rfind(".")
    last_comma = s.rfind(",")
    if last_dot > last_comma:
        s = s.replace(",", "")
    elif last_comma > last_dot:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def fetch_catalog():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        print("Loading shop page...")
        try:
            page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  goto warning: {e}")

        page.wait_for_timeout(4000)

        # Fire the DataTables AJAX call FROM INSIDE the browser
        # This uses the browser's session cookies automatically
        print("Firing DataTables AJAX from browser context...")
        result = page.evaluate("""async () => {
            // Get nonce from hidden input (wdtNonceFrontendEdit_4 or similar)
            const nonceInput = document.querySelector('[name*="wdtNonce"]');
            const nonce = nonceInput ? nonceInput.value : '';

            if (!nonce) return {error: 'No nonce found', html: document.body.innerHTML.slice(0,500)};

            const body = new URLSearchParams({
                draw: '1',
                'columns[0][data]': '0', 'columns[0][name]': 'Name',
                'columns[1][data]': '1', 'columns[1][name]': 'end_price',
                'columns[2][data]': '2', 'columns[2][name]': 'delta_D',
                'columns[3][data]': '3', 'columns[3][name]': 'delta_W',
                'columns[4][data]': '4', 'columns[4][name]': 'delta_M',
                'columns[5][data]': '5', 'columns[5][name]': 'delta_Y',
                'columns[6][data]': '6', 'columns[6][name]': 'spread',
                'columns[7][data]': '7', 'columns[7][name]': 'volatility',
                'order[0][column]': '1', 'order[0][dir]': 'desc',
                start: '0', length: '3000',
                'search[value]': '', 'search[regex]': 'false',
                wdtNonce: nonce
            });

            try {
                const resp = await fetch('/wp-admin/admin-ajax.php?action=get_wdtable&table_id=4', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'content-type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                    body: body.toString()
                });
                const text = await resp.text();
                return {nonce, status: resp.status, len: text.length, data: text};
            } catch(e) {
                return {error: e.toString(), nonce};
            }
        }""")

        browser.close()

    print(f"  nonce used: {result.get('nonce', 'N/A')}")
    print(f"  response status: {result.get('status', 'N/A')}")
    print(f"  response length: {result.get('len', 0)}")

    if 'error' in result:
        raise RuntimeError(f"Browser fetch error: {result['error']}")

    raw = result.get('data', '')
    if not raw or not raw.strip():
        raise RuntimeError("Empty response from in-browser AJAX call")

    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"JSON parse error: {e} â response preview: {raw[:200]}")

    rows = data.get('data', [])
    print(f"  rows received: {len(rows)}")
        if rows:
            print(f"  sample raw[0]: {repr(str(rows[0][0])[:300])}")

    if len(rows) < 10:
        raise RuntimeError(f"Too few rows: {len(rows)}")

    catalog = {}
    for row in rows:
        raw = str(row[0])
            import re as _re
            name = _re.sub(r'<[^>]+>', '', raw).strip()
        if name:
            catalog[name] = {
                "price": parse_num(row[1]), "day": parse_num(row[2]),
                "week": parse_num(row[3]), "month": parse_num(row[4]),
                "year": parse_num(row[5]), "spread": parse_num(row[6]),
                "vol": parse_num(row[7]),
            }

    return catalog


def apply_updates(catalog, today):
    items = json.loads((ROOT / "items.json").read_text())
    hist1 = json.loads((ROOT / "price_history_real_1.json").read_text())
    hist2 = json.loads((ROOT / "price_history_real_2.json").read_text())

    pinned_names = set(GIVEAWAY_PINNED.values())
    updated = 0
    skipped_pinned = 0

    for item in items:
        name = item["name"]
        if name in pinned_names:
            skipped_pinned += 1
            continue
        if name not in catalog or catalog[name]["price"] is None:
            continue
        rec = catalog[name]
        for k in ["price", "day", "week", "month", "year", "spread", "vol"]:
            if rec[k] is not None:
                item[k] = rec[k]
        if name in hist1:
            hist1[name][today] = rec["price"]
        elif name in hist2:
            hist2[name][today] = rec["price"]
        updated += 1

    print(f"  updated {updated} items")
    print(f"  protected {skipped_pinned} giveaway-pinned items")

    (ROOT / "items.json").write_text(
        json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    )
    (ROOT / "price_history_real_1.json").write_text(
        json.dumps(hist1, ensure_ascii=False, separators=(",", ":"))
    )
    (ROOT / "price_history_real_2.json").write_text(
        json.dumps(hist2, ensure_ascii=False, separators=(",", ":"))
    )
    return updated


def main():
    today = datetime.date.today().isoformat()
    print(f"=== Standoff 2 Price Update - {today} ===")
    try:
        catalog = fetch_catalog()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if len(catalog) < 100:
        print(f"ERROR: only {len(catalog)} items - aborting", file=sys.stderr)
        sys.exit(1)
    updated = apply_updates(catalog, today)
    if updated == 0:
        print("WARNING: no items matched")
        sys.exit(1)
    print(f"Done. {updated} items updated for {today}.")


if __name__ == "__main__":
    main()
