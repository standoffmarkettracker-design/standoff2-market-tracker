#!/usr/bin/env python3
"""
Compares current items.json to the previous git commit.
Sends a Discord alert for any item that doubled in price.
"""
import json, subprocess, urllib.request, sys

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1521212880614330488/whaIWiT5y7FbVYQ93k0TwHtbPljg5CxdG8vwr8J9s6vbWTturZluUyKhHTNKcL0Ytzj5"
PINNED = {'Sticker "Province"', 'AKR "Scylla" StatTrack', 'S2 Mantis "Ink Wash"'}

try:
    prev_raw = subprocess.check_output(["git", "show", "HEAD~1:items.json"], stderr=subprocess.DEVNULL)
    prev_items = json.loads(prev_raw)
except Exception as e:
    print(f"Could not get previous items.json: {e}")
    sys.exit(0)

with open("items.json") as f:
    curr_items = json.load(f)

prev = {i["name"]: i.get("price", 0) for i in prev_items if i.get("price", 0) > 0}
curr = {i["name"]: i.get("price", 0) for i in curr_items if i.get("price", 0) > 0}

doubled = []
for name, new_price in curr.items():
    if name in PINNED:
        continue
    old_price = prev.get(name, 0)
    if old_price > 0 and new_price >= old_price * 2:
        pct = ((new_price - old_price) / old_price) * 100
        doubled.append((name, old_price, new_price, pct))

doubled.sort(key=lambda x: x[3], reverse=True)

if not doubled:
    print("No items doubled in price.")
    sys.exit(0)

print(f"Found {len(doubled)} item(s) that doubled:")
for name, old, new, pct in doubled:
    print(f"  {name}: {old:.2f} -> {new:.2f} (+{pct:.0f}%)")

lines = [f"\U0001f680 **{name}**: {old:.2f}G \u2192 **{new:.2f}G** (+{pct:.0f}%)" for name, old, new, pct in doubled[:20]]
content = "## \U0001f4c8 Price Alert \u2014 Items that doubled since last check!\n" + "\n".join(lines)
if len(doubled) > 20:
    content += f"\n_...and {len(doubled) - 20} more_"

payload = json.dumps({"content": content}).encode()
req = urllib.request.Request(DISCORD_WEBHOOK, data=payload, method="POST", headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as r:
        print(f"Discord notified: {r.status}")
except Exception as e:
    print(f"Discord error: {e}")
    sys.exit(1)
