#!/usr/bin/env python3
"""
Standoff 2 Market Tracker - Daily Price Update Script
Fetches prices from standoff-2.com and updates items.json and price history files.
Run: python scripts/update_prices.py
"""

import json, re, sys, datetime, requests
from pathlib import Path

ROOT = Path(__file__).parent.parent

HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://standoff-2.com",
    "referer": "https://standoff-2.com/shop/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-requested-with": "XMLHttpRequest",
}

SHOP_URL = "https://standoff-2.com/shop/"
AJAX_URL = "https://standoff-2.com/wp-admin/admin-ajax.php"
TABLE_ID = "2"
PAGE_SIZE = 3000

GIVEAWAY_PINNED = {
    "weekly": 'Sticker "Province"',
    "biweekly": 'AKR "Scylla" StatTrack',
    "monthly": 'S2 Mantis "Ink Wash"',
}


def get_nonce(session):
    """Load shop page and extract DataTables nonce."""
    print("Fetching nonce from shop page...")
    r = session.get(SHOP_URL, headers={"user-agent": HEADERS["user-agent"]}, timeout=30)
    r.raise_for_status()
    m = re.search(r'"wdtNonce"\s*:\s*"([^"]+)"', r.text)
    if not m:
        m = re.search(r'wdtNonce["\s:]+([a-f0-9]{8,12})', r.text)
    if not m:
        raise RuntimeError("Could not find DataTables nonce in shop page")
    nonce = m.group(1)
    print(f"  nonce: {nonce}")
    return nonce


def fetch_catalog(session, nonce):
    """Fetch all items from DataTables endpoint."""
    print("Fetching full catalog...")
    body = (
        f"draw=1"
        f"&columns%5B0%5D%5Bdata%5D=0&columns%5B0%5D%5Bname%5D=Name"
        f"&columns%5B1%5D%5Bdata%5D=1&columns%5B1%5D%5Bname%5D=end_price"
        f"&columns%5B2%5D%5Bdata%5D=2&columns%5B2%5D%5Bname%5D=delta_D"
        f"&columns%5B3%5D%5Bdata%5D=3&columns%5B3%5D%5Bname%5D=delta_W"
        f"&columns%5B4%5D%5Bdata%5D=4&columns%5B4%5D%5Bname%5D=delta_M"
        f"&columns%5B5%5D%5Bdata%5D=5&columns%5B5%5D%5Bname%5D=delta_Y"
        f"&columns%5B6%5D%5Bdata%5D=6&columns%5B6%5D%5Bname%5D=spread"
        f"&columns%5B7%5D%5Bdata%5D=7&columns%5B7%5D%5Bname%5D=volatility"
        f"&order%5B0%5D%5Bcolumn%5D=1&order%5B0%5D%5Bdir%5D=desc"
        f"&start=0&length={PAGE_SIZE}"
        f"&search%5Bvalue%5D=&search%5Bregex%5D=false"
        f"&wdtNonce={nonce}"
    )
    r = session.post(f"{AJAX_URL}?action=get_wdtable&table_id={TABLE_ID}", data=body, headers=HEADERS, timeout=60)
    r.raise_for_status()
    rows = r.json().get("data", [])
    print(f"  received {len(rows)} rows")

    def parse_num(val):
        if val is None: return None
        s = re.sub(r"[^\d.\-]", "", str(val).replace(".", "").replace(",", "."))
        try: return float(s)
        except ValueError: return None

    catalog = {}
    for row in rows:
        name = str(row[0]).strip()
        if name:
            catalog[name] = {
                "price": parse_num(row[1]), "day": parse_num(row[2]),
                "week": parse_num(row[3]), "month": parse_num(row[4]),
                "year": parse_num(row[5]), "spread": parse_num(row[6]),
                "vol": parse_num(row[7]),
            }
    return catalog


def apply_updates(catalog, today):
    """Update items.json and price history files."""
    items = json.loads((ROOT / "items.json").read_text())
    hist1 = json.loads((ROOT / "price_history_real_1.json").read_text())
    hist2 = json.loads((ROOT / "price_history_real_2.json").read_text())

    updated = 0
    for item in items:
        name = item["name"]
        if name not in catalog or catalog[name]["price"] is None:
            continue
        rec = catalog[name]
        for k in ["price", "day", "week", "month", "year", "spread", "vol"]:
            if rec[k] is not None: item[k] = rec[k]
        if name in hist1: hist1[name][today] = rec["price"]
        elif name in hist2: hist2[name][today] = rec["price"]
        updated += 1

    print(f"  updated {updated} items")
    (ROOT / "items.json").write_text(json.dumps(items, ensure_ascii=False, separators=(",", ":")))
    (ROOT / "price_history_real_1.json").write_text(json.dumps(hist1, ensure_ascii=False, separators=(",", ":")))
    (ROOT / "price_history_real_2.json").write_text(json.dumps(hist2, ensure_ascii=False, separators=(",", ":")))
    return updated


def main():
    today = datetime.date.today().isoformat()
    print(f"=== Standoff 2 Price Update - {today} ===")
    session = requests.Session()
    try:
        nonce = get_nonce(session)
        catalog = fetch_catalog(session, nonce)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if len(catalog) < 100:
        print(f"ERROR: only {len(catalog)} items — aborting", file=sys.stderr)
        sys.exit(1)
    updated = apply_updates(catalog, today)
    if updated == 0:
        print("WARNING: no items updated")
        sys.exit(1)
    print(f"Done. {updated} items updated for {today}.")


if __name__ == "__main__":
    main()
