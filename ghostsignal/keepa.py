"""Keepa API client — the legit firehose for Amazon price/rank/offer history.

Requires a paid Keepa API key (https://keepa.com/#!api) in KEEPA_API_KEY.
Each product lookup costs tokens; storefront pulls cost more. We batch 100 ASINs
per request (Keepa's max) and store every response as a snapshot.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from . import db

API = "https://api.keepa.com"
DOMAIN_US = 1

# Indexes into Keepa's csv / stats arrays.
AMAZON, NEW, SALES_RANK, COUNT_NEW, BUY_BOX = 0, 1, 3, 11, 18


class KeepaError(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("KEEPA_API_KEY")
    if not key:
        raise KeepaError("Set KEEPA_API_KEY to use Keepa (https://keepa.com/#!api)")
    return key


def _get(endpoint: str, **params) -> dict:
    params = {"key": _key(), "domain": DOMAIN_US, **params}
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read())
    if data.get("error"):
        raise KeepaError(str(data["error"]))
    return data


def _cents(v) -> float | None:
    return round(v / 100, 2) if isinstance(v, (int, float)) and v >= 0 else None


def _int(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and v >= 0 else None


def _at(arr, i):
    return arr[i] if isinstance(arr, list) and len(arr) > i else None


def parse_product(p: dict) -> tuple[dict, dict]:
    """Map a Keepa product object -> (product fields, snapshot fields)."""
    stats = p.get("stats") or {}
    cur, avg90 = stats.get("current") or [], stats.get("avg90") or []
    images = (p.get("imagesCSV") or "").split(",")
    cats = p.get("categoryTree") or []
    fees = p.get("fbaFees") or {}
    ref = p.get("referralFeePercentage") or p.get("referralFeePercent")

    buy_box = _cents(stats.get("buyBoxPrice")) or _cents(_at(cur, BUY_BOX))
    product = {
        "title": p.get("title"),
        "brand": p.get("brand"),
        "category": cats[0]["name"] if cats else None,
        "upc": (p.get("upcList") or [None])[0],
        "image_url": f"https://m.media-amazon.com/images/I/{images[0]}" if images and images[0] else None,
    }
    snapshot = {
        "buy_box": buy_box,
        "amazon_price": _cents(_at(cur, AMAZON)),
        "avg_price_90": _cents(_at(avg90, BUY_BOX)) or _cents(_at(avg90, NEW)),
        "sales_rank": _int(_at(cur, SALES_RANK)),
        "monthly_sold": _int(p.get("monthlySold")),
        "offer_count": _int(_at(cur, COUNT_NEW)),
        "referral_pct": ref / 100 if isinstance(ref, (int, float)) and ref > 0 else None,
        "fba_fee": _cents(fees.get("pickAndPackFee")),
    }
    return product, snapshot


def refresh(conn, asins: list[str], source: str = "keepa") -> dict:
    """Pull fresh Keepa data for ASINs and store snapshots. Returns token usage."""
    done, tokens_left = 0, None
    for i in range(0, len(asins), 100):
        batch = asins[i:i + 100]
        data = _get("product", asin=",".join(batch), stats=90, buybox=1)
        tokens_left = data.get("tokensLeft")
        for p in data.get("products") or []:
            asin = p.get("asin")
            if not asin:
                continue
            product, snap = parse_product(p)
            db.upsert_product(conn, asin, **product)
            db.add_snapshot(conn, asin, source, raw_json=None, **snap)
            done += 1
        conn.commit()
    return {"updated": done, "tokens_left": tokens_left}


def seller_storefront(conn, seller_id: str) -> list[str]:
    """Shadow a seller: pull the ASINs on their storefront and add new ones to the DB."""
    data = _get("seller", seller=seller_id, storefront=1)
    seller = (data.get("sellers") or {}).get(seller_id) or {}
    asins = seller.get("asinList") or []
    for asin in asins:
        db.upsert_product(conn, asin, origin=f"seller:{seller_id}")
    conn.execute(
        """INSERT INTO tracked_sellers (seller_id, name, added_at, last_pull) VALUES (?, ?, ?, ?)
           ON CONFLICT(seller_id) DO UPDATE SET last_pull = excluded.last_pull, name = COALESCE(excluded.name, name)""",
        (seller_id, seller.get("sellerName"), db.now(), db.now()),
    )
    conn.commit()
    return asins
