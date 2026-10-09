#!/usr/bin/env python3
"""Run on the home Mac once a day: look up store prices by barcode (UPCitemdb's free 100/day is per internet
address, and Railway's shared address is always used up), then send the results to GhostSignal.

    GHOSTSIGNAL_URL=https://... GHOSTSIGNAL_PASSWORD=... python3 tools/barcode_prices.py
"""
import json, os, sys, time, urllib.error, urllib.request

BASE = os.environ.get("GHOSTSIGNAL_URL", "https://ghostsignal-production.up.railway.app").rstrip("/")
AUTH = {"Authorization": "Bearer " + os.environ["GHOSTSIGNAL_PASSWORD"], "Content-Type": "application/json"}


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=AUTH)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def lookup(upc):
    req = urllib.request.Request("https://api.upcitemdb.com/prod/trial/lookup?upc=" + upc,
                                 headers={"User-Agent": "GhostSignal/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("items") or [], r.headers.get("X-RateLimit-Remaining")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "0"
        if e.code in (400, 404):
            return [], None
        raise


due = call(f"/api/upcdb/due?limit={int(sys.argv[1]) if len(sys.argv) > 1 else 95}")
results, done = [], 0
for row in due:
    items, left = lookup(row["upc"])
    if items is None:
        print("daily limit reached"); break
    results.append({"asin": row["asin"], "items": items}); done += 1
    if len(results) >= 10:
        print(call("/api/upcdb/results", {"results": results})); results = []
    time.sleep(2)
if results:
    print(call("/api/upcdb/results", {"results": results}))
print(f"looked up {done} of {len(due)}")
