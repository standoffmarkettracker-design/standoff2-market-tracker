#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
Standoff 2 BlueStacks Allowance Bot
Uses ADB to control BlueStacks + Claude vision to find and buy items
in the real Standoff 2 in-game marketplace.

Setup:
  pip install anthropic pillow
  adb connect localhost:5555

Run:
  python bluestacks_bot.py
"""

import os, json, time, base64, subprocess, urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "anthropic"])
    import anthropic

try:
    from PIL import Image
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pillow"])
    from PIL import Image

FIREBASE_URL = "https://standoff-2-tracker-default-rtdb.firebaseio.com"
ADB_DEVICE   = "localhost:5555"   # auto-detected below
LOG_FILE     = Path("bluestacks_bot.log")
API_KEY      = os.environ.get("ANTHROPIC_API_KEY") or (open("api_key.txt").read().strip() if __import__('pathlib').Path("api_key.txt").exists() else "")
POLL_SECS    = 5

def detect_adb_device():
    """Auto-detect the BlueStacks ADB device and set global ADB_DEVICE."""
    global ADB_DEVICE
    result = subprocess.run("adb devices", shell=True, capture_output=True, text=True).stdout
    for line in result.splitlines():
        if "device" in line and "List" not in line:
            device = line.split()[0]
            ADB_DEVICE = device
            log(f"Auto-detected ADB device: {device}")
            return device
    log("WARNING: No ADB device found, defaulting to localhost:5555")
    return "localhost:5555"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    # Strip all non-ASCII characters to avoid Windows console encoding errors
    safe_msg = msg.encode("ascii", "replace").decode("ascii")
    line = f"[{ts}] {safe_msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def fb_get(path):
    try:
        with urllib.request.urlopen(f"{FIREBASE_URL}/{path}.json") as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"FB GET error: {e}")
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
        log(f"FB PATCH error: {e}")

def adb(cmd):
    full = f"adb -s {ADB_DEVICE} {cmd}"
    result = subprocess.run(full, shell=True, capture_output=True, text=True)
    return result.stdout.strip()

def adb_screenshot():
    global ADB_DEVICE
    # Try screencap
    out = adb("shell screencap -p /sdcard/s2_bot_cap.png")
    if "error" in out.lower() or "failed" in out.lower():
        log(f"  screencap warning: {out}")
    pull = adb("pull /sdcard/s2_bot_cap.png s2_cap.png")
    if not Path("s2_cap.png").exists():
        raise RuntimeError(f"Screenshot pull failed: {pull}")
    with open("s2_cap.png", "rb") as f:
        data = f.read()
    if len(data) < 1000:
        raise RuntimeError(f"Screenshot too small ({len(data)} bytes) — ADB may not be connected")
    log(f"  Screenshot: {len(data)//1024}KB")
    return base64.standard_b64encode(data).decode()

def adb_tap(x, y):
    adb(f"shell input tap {x} {y}")
    log(f"  Tapped ({x}, {y})")
    time.sleep(0.5)

def adb_swipe(x1, y1, x2, y2, duration_ms=500):
    adb(f"shell input swipe {x1} {y1} {x2} {y2} {duration_ms}")
    time.sleep(0.5)

def adb_type(text):
    safe = text.replace(" ", "%s").replace("'", "\\'")
    adb(f"shell input text '{safe}'")
    time.sleep(0.3)

def ask_claude(prompt, screenshot_b64):
    client = anthropic.Anthropic(api_key=API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64}},
            {"type": "text", "text": prompt}
        ]}]
    )
    return resp.content[0].text.strip()

def get_tap_coords(prompt, screenshot_b64):
    full_prompt = prompt + "\n\nRespond with ONLY x,y pixel coordinates like: 540,960\nIf not found, respond: NOT_FOUND"
    result = ask_claude(full_prompt, screenshot_b64)
    log(f"  Claude: {result}")
    if "NOT_FOUND" in result:
        return None
    try:
        parts = result.strip().split(",")
        return int(parts[0].strip()), int(parts[1].strip())
    except Exception:
        return None

def find_and_buy_ingame(seller, item_name, list_price):
    log(f"Starting in-game purchase: '{item_name}' by '{seller}' at {list_price:.2f}G")

    log("Step 1: Taking screenshot...")
    shot = adb_screenshot()
    state = ask_claude("What screen is currently showing in this Standoff 2 app? Is the marketplace/shop open? One sentence.", shot)
    log(f"  Current state: {state}")

    log("Step 2: Navigating to marketplace...")
    shot = adb_screenshot()
    coords = get_tap_coords(
        "This is Standoff 2 mobile game. Find the MARKET or SHOP tab/button - it could be in the bottom navigation bar, "
        "a shopping cart icon, or a tab labeled Market/Shop/Store. Give coordinates to tap it.",
        shot
    )
    if coords:
        adb_tap(*coords)
        time.sleep(3)
        log("  Tapped market button")
    else:
        log("  Market button not found — may already be there")

    log(f"Step 3: Searching for '{item_name}'...")
    shot = adb_screenshot()
    search_coords = get_tap_coords(
        "Find the search bar or search icon in this Standoff 2 marketplace screenshot. Usually a text input or magnifying glass.",
        shot
    )
    if search_coords:
        adb_tap(*search_coords)
        time.sleep(1)
        search_term = item_name.split('"')[0].strip()
        adb_type(search_term)
        time.sleep(2)
        shot = adb_screenshot()
        log(f"  Searched for: {search_term}")
    else:
        log("  No search bar found, scanning visible listings")

    log(f"Step 4: Looking for listing by {seller} at {list_price:.2f}G...")
    shot = adb_screenshot()
    listing_coords = get_tap_coords(
        f"In this Standoff 2 marketplace, find a listing for '{item_name}' by seller '{seller}' at {list_price:.2f} gold. Tap coordinates:",
        shot
    )

    if not listing_coords:
        log("  Scrolling to search...")
        adb_swipe(540, 800, 540, 400, 500)
        time.sleep(1)
        shot = adb_screenshot()
        listing_coords = get_tap_coords(
            f"Find listing for '{item_name}' by '{seller}' at {list_price:.2f} gold.",
            shot
        )

    if not listing_coords:
        log("Could not find the listing after scrolling")
        return False

    log(f"Step 5: Tapping listing at {listing_coords}...")
    adb_tap(*listing_coords)
    time.sleep(2)
    shot = adb_screenshot()

    log("Step 6: Looking for Buy button...")
    buy_coords = get_tap_coords(
        "Find the 'Buy' button in this Standoff 2 item detail screen. Usually a prominent colored button at the bottom.",
        shot
    )
    if not buy_coords:
        log("Could not find Buy button")
        return False

    log(f"  Tapping Buy at {buy_coords}...")
    adb_tap(*buy_coords)
    time.sleep(2)
    shot = adb_screenshot()

    log("Step 7: Checking for confirmation dialog...")
    confirm = ask_claude("Is there a purchase confirmation dialog? Look for Confirm/Buy/OK buttons. Answer YES or NO.", shot)
    log(f"  Confirmation needed: {confirm}")

    if "YES" in confirm.upper():
        confirm_coords = get_tap_coords("Find the confirmation/OK/Confirm button to complete this purchase.", shot)
        if confirm_coords:
            adb_tap(*confirm_coords)
            time.sleep(2)
            log("  Confirmed!")

    log("Step 8: Verifying purchase...")
    shot = adb_screenshot()
    with open("purchase_result.png", "wb") as f:
        f.write(base64.b64decode(shot))
    result = ask_claude(
        "Did the purchase just succeed in this Standoff 2 screenshot? Look for success messages or gold deduction. Answer SUCCEEDED or FAILED and briefly why.",
        shot
    )
    log(f"  Result: {result}")
    return "SUCCEEDED" in result.upper()


def process_claim(uid, claim):
    seller     = claim.get("username", "")
    item_name  = claim.get("itemName", "")
    list_price = float(claim.get("listPrice", 0))
    allowance  = int(claim.get("allowance", 0))
    tier       = claim.get("tier", "Basic")

    log(f"\n{'='*55}")
    log(f"Claim: {uid[:12]}... | {tier} | {allowance}G | {seller} | {item_name} | {list_price:.2f}G")
    log(f"{'='*55}")

    if not seller or not item_name or not list_price:
        log("Incomplete data — skipping")
        fb_patch(f"allowanceClaims/{uid}", {"status": "error", "error": "incomplete_data"})
        return

    fb_patch(f"allowanceClaims/{uid}", {"status": "processing"})
    try:
        success = find_and_buy_ingame(seller, item_name, list_price)
    except Exception as e:
        safe_e = str(e).encode("ascii","replace").decode()
        log(f"Purchase error: {safe_e}")
        import traceback
        tb = traceback.format_exc().encode("ascii","replace").decode()
        log(tb)
        success = False

    if success:
        fb_patch(f"allowanceClaims/{uid}", {
            "status": "complete",
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "lastClaimDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "error": None,
        })
        log(f"SUCCESS — {allowance}G sent to {seller}")
    else:
        fb_patch(f"allowanceClaims/{uid}", {"status": "failed", "error": "listing_not_found_in_game"})
        log(f"FAILED for {uid[:12]}...")


def main():
    if not API_KEY:
        print("\nERROR: No API key found.")
        print("Create a file called api_key.txt in this folder with just your key inside.")
        sys.exit(1)

    detect_adb_device()
    log("="*55)
    log("  Standoff 2 BlueStacks Bot (ADB + Claude Vision)")
    log("="*55)
    log(f"ADB: {ADB_DEVICE} | Polling every {POLL_SECS}s | Ctrl+C to stop\n")

    processed = set()
    while True:
        try:
            claims = fb_get("allowanceClaims")
            if claims:
                for uid, claim in claims.items():
                    if claim.get("status") == "pending" and uid not in processed:
                        processed.add(uid)
                        process_claim(uid, claim)
            else:
                log("No pending claims...")
        except Exception as e:
            safe_e = str(e).encode("ascii","replace").decode()
            log(f"Poll error: {safe_e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot stopped.")
