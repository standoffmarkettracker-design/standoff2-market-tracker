#!/usr/bin/env python3
"""
Standoff 2 Market Tracker - Daily Price Update Script (Playwright version)
Uses a real browser to intercept DataTables API calls from standoff-2.com.
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

    catalog = {}
    api_responses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()

        def handle_response(response):
            if "admin-ajax.php" in response.url and response.status == 200:
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="ignore").strip()
                    if text.startswith("{") or text.startswith("["):
                        data = json.loads(text)
                        if isinstance(data, dict) and "data" in data and len(data["data"]) > 10:
                            api_responses.append(data)
                            print(f"  intercepted: {len(data['data'])} rows from {response.url}")
                except Exception as ex:
                    print(f"  response parse error: {ex}")

        page.on("response", handle_response)

        print("Navigating to shop page...")
        try:
            page.goto(SHOP_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        print("Waiting for DataTables to load...")
        page.wait_for_timeout(8000)

        # Scroll to trigger any lazy-loaded content
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(3000)

        # Try to find and click a DataTables element if present
        try:
            page.wait_for_selector("table.dataTable, .wpdatatables-wrapper", timeout=5000)
            print("  DataTable found on page")
        except Exception:
            print("  No DataTable selector found, waiting more...")
            page.wait_for_timeout(5000)

        browser.close()

    print(f"Total API responses captured: {len(api_responses)}")

    if not api_responses:
        raise RuntimeError("No DataTables API response intercepted from shop page")

    rows = max(api_responses, key=lambda r: len(r["data"]))["data"]
    print(f"Using response with {len(rows)} rows")

    for row in rows:
        name = str(row[0]).strip()
        if name:
            catalog[name] = {
                "price": parse_num(row[1]),
                "day":   parse_num(row[2]),
                "week":  parse_num(row[3]),
                "month": parse_num(row[4]),
                "year":  parse_num(row[5]),
                "spread":parse_num(row[6]),
                "vol":   parse_num(row[7]),
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
        print("WARNING: no items updated")
        sys.exit(1)
    print(f"Done. {updated} items updated for {today}.")


if __name__ == "__main__":
    main()
