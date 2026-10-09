"""Free store prices by barcode (UPCitemdb trial API: 100 lookups/day, no key).

SerpAPI's free plan (250 searches/month) can only check a handful of products, so most leads never got a store
price. UPCitemdb looks a product up by its UPC, so there is no guessing about size or variant, and returns the
prices it has recorded at Walmart, Target, Walgreens, Lowe's and others.

Its prices can be old, so only recent offers (default 90 days) from real retailers are kept, and each one is
labelled "price may be old": it puts a product on the Check list, it never makes a buy on its own.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import db, stores

API = "https://api.upcitemdb.com/prod/trial/lookup?upc="
DAILY = 95                 # the trial allows 100 a day; keep a few for manual lookups
MAX_AGE_DAYS = 90
_PACK = re.compile(r"\b(?:pack|set|case|box)\s+of\s+(\d+)\b|\b(\d+)[\s-]?(?:pack|pk)\b", re.I)


def _pack(title: str) -> int:
    nums = [int(a or b) for a, b in _PACK.findall(title or "")]
    return max(nums) if nums else 1


def lookup(upc: str) -> tuple[list[dict], int | None]:
    """-> (items, lookups remaining today). Raises RuntimeError('limit') when the daily quota is used up."""
    req = urllib.request.Request(API + upc, headers={"User-Agent": "GhostSignal/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            left = r.headers.get("X-RateLimit-Remaining")
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("limit") from e
        if e.code == 404:
            return [], None
        raise
    return data.get("items") or [], int(left) if left and left.isdigit() else None


def offers_for(item: dict, amazon_title: str, max_age_days: int = MAX_AGE_DAYS) -> list[dict]:
    """Recent new-condition offers from real retailers, priced for the Amazon listing's pack size."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp()
    qty = 1
    pa, pu = _pack(amazon_title), _pack(item.get("title") or "")
    if pa > pu and pa % pu == 0:          # Amazon multipack carrying the single unit's barcode
        qty = pa // pu
    best: dict[str, dict] = {}
    for o in item.get("offers") or []:
        key = stores.store_key(o.get("merchant") or "")
        price, ts = o.get("price"), o.get("updated_t") or 0
        if not key or key not in stores.RETAILERS_OK or not price or price <= 0 or ts < cutoff:
            continue
        if (o.get("condition") or "New").lower() != "new":
            continue
        if key not in best or price < best[key]["price"]:
            best[key] = {"retailer": key, "price": float(price), "pack_qty": qty, "url": o.get("link") or "",
                         "seen": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), "title": item.get("title") or ""}
    return sorted(best.values(), key=lambda x: x["price"] * x["pack_qty"])


def _used_today(conn) -> int:
    raw = db.get_setting(conn, "upcdb_day") or ""
    day, _, n = raw.partition(":")
    return int(n or 0) if day == date.today().isoformat() else 0


def _mark_used(conn, n: int) -> None:
    db.set_setting(conn, "upcdb_day", f"{date.today().isoformat()}:{_used_today(conn) + n}")


def due(conn, limit: int = DAILY, days: float = 21) -> list[str]:
    """Same candidates as the SerpAPI check (sellable or one approval away, real brand, $18+, ranked), with a UPC."""
    from . import engine
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    checked = {r[0][len("upc_checked:"):]: r[1] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'upc_checked:%'")}
    picks = []
    for (asin,) in conn.execute("""SELECT asin FROM products WHERE status NOT IN ('dead','pass')
                                   AND title IS NOT NULL AND upc IS NOT NULL AND upc != ''"""):
        if checked.get(asin, "") >= cutoff:
            continue
        v = engine.list_view(conn, asin)
        s, e = v["snapshot"] or {}, v["economics"]
        if v["restricted"] or v["gated"] not in ("ungated", "approval") or v.get("import_brand"):
            continue
        if (v.get("enrichment") or {}).get("sold_in_stores") is False:
            continue
        if s.get("amazon_price") or not s.get("sales_rank") or s["sales_rank"] > 80_000 or (e.get("sale_price") or 0) < 18:
            continue
        picks.append(((1 if v["gated"] == "ungated" else 0), (v.get("new_sellers") or 0), -s["sales_rank"], asin))
    return [a for *_, a in sorted(picks, reverse=True)[:limit]]


def find_prices(conn, asins: list[str] | None = None, limit: int | None = None) -> dict:
    budget = max(0, (limit or DAILY) - _used_today(conn))
    asins = (asins if asins is not None else due(conn, budget))[:budget]
    looked = saved = 0
    for asin in asins:
        p = conn.execute("SELECT title, upc FROM products WHERE asin = ?", (asin,)).fetchone()
        if not p or not p["upc"]:
            continue
        try:
            items, left = lookup(p["upc"])
        except RuntimeError:
            break                          # daily quota used up
        except Exception as e:             # network hiccup: skip this one
            print(f"  upcdb {asin}: {str(e)[:80]}", flush=True)
            continue
        looked += 1
        _mark_used(conn, 1)
        conn.execute("DELETE FROM retail_sources WHERE asin = ? AND note LIKE 'barcode%'", (asin,))
        for item in items[:1]:
            for o in offers_for(item, p["title"])[:6]:
                db.add_source(conn, asin, o["retailer"], o["price"], pack_qty=o["pack_qty"], url=o["url"],
                              note=f"barcode match · same size · price from {o['seen']}, may be old"
                                   + (f" · buy {o['pack_qty']}" if o["pack_qty"] > 1 else "") + f": {o['title'][:100]}")
                saved += 1
        db.set_setting(conn, f"upc_checked:{asin}", db.now())
        conn.commit()
        if left is not None and left <= 100 - DAILY:
            break
        time.sleep(1.2)                    # the trial allows about 6 requests a minute in bursts
    return {"looked_up": looked, "prices_saved": saved, "used_today": _used_today(conn)}
