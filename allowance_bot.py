#!/usr/bin/env python3
"""
Standoff 2 Gold Allowance Bot
Watches Firebase for pending claims, buys the listed item on standoff-2.com,
then marks the claim complete so the user gets their gold.

Run:
  python allowance_bot.py

First run opens a browser - log in with Google (your main account),
then press ENTER. Session saved to session.json for future runs.
"""

import asyncio, json, sys, urllib.request
from pathlib import Path
from datetime import datetime, timezone

FIREBASE_URL = "https://standoff-2-tracker-default-rtdb.firebaseio.com"
SESSION_FILE = Path("session.json")
MARKET_URL   = "https://standoff-2.com/en/market"
BASE_URL     = "https://standoff-2.com"
LOG_FILE     = Path("allowance_bot.log")
POLL_SECS    = 5

def log(msg):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def fb_get(path):
    try:
        with urllib.request.urlopen(f"{FIREBASE_URL}/{path}.json") as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"FB GET error {path}: {e}")
        return None

def fb_patch(path, data):
    req = urllib.request.Request(
        f"{FIREBASE_URL}/{path}.json",
        data=json.dumps(data).encode(), method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"FB PATCH error {path}: {e}")

def load_cookies():
    if SESSION_FILE.exists():
        with open(SESSION_FILE) as f: return json.load(f)
    return None

def save_cookies(cookies):
    with open(SESSION_FILE, "w") as f: json.dump(cookies, f, indent=2)
    log(f"Session saved -> {SESSION_FILE}")

async def ensure_logged_in(page, context):
    await page.goto(BASE_URL + "/shop/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    try:
        await page.locator("a[href*='/profile'], [class*='avatar'], [class*='profile'], .user-name").first.wait_for(timeout=4000)
        log("Already logged in")
        return
    except Exception:
        pass
    print()
    print("=" * 60)
    print("  Browser window is open.")
    print("  Log in with Google (your main account), then")
    print("  press ENTER here to continue.")
    print("=" * 60)
    input("  > ")
    save_cookies(await context.cookies())
    log("Logged in - session saved")

async def find_and_buy(page, seller, item_name, list_price):
    log(f"Searching: '{item_name}' by '{seller}' at {list_price:.2f} G")
    await page.goto(MARKET_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    try:
        inp = page.locator("input[placeholder*='search' i], input[placeholder*='item' i], input[type='search']").first
        await inp.fill(item_name.split('"')[0].strip(), timeout=5000)
        await page.wait_for_timeout(2000)
    except Exception:
        log("No search input - scanning page")
    await page.screenshot(path="bot_market.png")
    item_hint = item_name.split('"')[0].lower() if '"' in item_name else item_name.lower()
    check = await page.evaluate(f"""() => {{
        const body = document.body.innerText;
        return {{
            hasItem:   body.toLowerCase().includes('{item_hint}'),
            hasPrice:  body.includes('{list_price:.2f}'),
            hasSeller: body.toLowerCase().includes('{seller.lower()}'),
        }};
    }}""")
    log(f"Check -> item:{check['hasItem']} price:{check['hasPrice']} seller:{check['hasSeller']}")
    if not check["hasItem"]:
        log("Item not found on market page"); return False
    if not check["hasPrice"]:
        log(f"Price {list_price:.2f} G not visible - listing may not be live yet"); return False
    try:
        buy = page.get_by_role("button", name="Buy", exact=False).first
        await buy.wait_for(timeout=5000)
        await buy.click()
        await page.wait_for_timeout(2000)
        await page.screenshot(path="bot_purchase.png")
        log("Buy clicked - screenshot: bot_purchase.png")
        return True
    except Exception as e:
        log(f"Buy button error: {e}")
        await page.screenshot(path="bot_error.png")
        return False

async def process_claim(page, context, uid, claim):
    seller     = claim.get("username", "")
    item_name  = claim.get("itemName", "")
    list_price = float(claim.get("listPrice", 0))
    allowance  = int(claim.get("allowance", 0))
    tier       = claim.get("tier", "Basic")
    log(f"Claim: uid={uid[:8]}... tier={tier} {allowance}G seller={seller} item={item_name} price={list_price:.2f}G")
    if not seller or not item_name or not list_price:
        log("Incomplete claim data")
        fb_patch(f"allowanceClaims/{uid}", {"status": "error", "error": "incomplete_data"})
        return
    fb_patch(f"allowanceClaims/{uid}", {"status": "processing"})
    success = await find_and_buy(page, seller, item_name, list_price)
    if success:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fb_patch(f"allowanceClaims/{uid}", {
            "status": "complete", "completedAt": datetime.now(timezone.utc).isoformat(),
            "lastClaimDate": today, "error": None,
        })
        log(f"Claim complete for {uid[:8]}... - {allowance}G sent")
    else:
        fb_patch(f"allowanceClaims/{uid}", {"status": "failed", "error": "listing_not_found"})
        log(f"Claim failed for {uid[:8]}...")

async def main():
    from playwright.async_api import async_playwright
    log("=" * 60)
    log("  Standoff 2 Allowance Bot")
    log("=" * 60)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        cookies = load_cookies()
        if cookies:
            await context.add_cookies(cookies)
            log("Loaded saved session")
        page = await context.new_page()
        await ensure_logged_in(page, context)
        log(f"\nWatching Firebase every {POLL_SECS}s. Press Ctrl+C to stop.\n")
        processed = set()
        while True:
            try:
                claims = fb_get("allowanceClaims")
                if claims:
                    for uid, claim in claims.items():
                        if claim.get("status") == "pending" and uid not in processed:
                            processed.add(uid)
                            await process_claim(page, context, uid, claim)
            except Exception as e:
                log(f"Poll error: {e}")
            await asyncio.sleep(POLL_SECS)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
