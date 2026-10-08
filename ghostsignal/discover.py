"""Find new products to evaluate by searching Amazon's catalog (Seller API, free).

Your own orders only show what three people buy. Arbitrage needs products that are (a) sold in real stores, so
there is somewhere to buy them, (b) in categories you're approved for, and (c) selling well. This seeds the
database with exactly those: searches for brands that Walmart/Target/Costco carry, in your open categories.
Everything added goes through the normal pipeline (market data, gating, junk filter, scoring) on the next run.
"""

from __future__ import annotations

import time

from . import db, spapi

# Brands that big-box stores stock, grouped by the categories you're approved for or close to.
SEEDS: dict[str, list[str]] = {
    "grocery": ["Hershey's", "Lindt", "Ghirardelli", "Haribo", "Trolli", "Skittles", "M&M'S", "Reese's", "Kinder",
                "Nature Valley", "KIND", "RXBAR", "Quest Nutrition", "Pringles", "Takis", "Goldfish", "Annie's",
                "Kellogg's", "Pop-Tarts", "Celsius", "Liquid I.V.", "Poppi", "Olipop", "Starbucks", "Peet's Coffee",
                "Dunkin'", "McCormick", "Torani", "Swiss Miss", "Jif", "Smucker's", "Bob's Red Mill", "Pocky", "Hi-Chew",
                "Welch's", "Sour Patch Kids", "Nerds", "Jolly Rancher", "Twizzlers", "Ferrero Rocher"],
    "books": ["Scholastic", "Penguin", "HarperCollins", "Simon & Schuster", "Disney Press", "DK", "Golden Books",
              "Workman Publishing", "Highlights", "Little Golden Book", "Dr. Seuss", "Pete the Cat"],
    "toys_plush": ["Squishmallows", "Jellycat", "Ty", "GUND", "Melissa & Doug", "Crayola", "Play-Doh", "Hasbro Gaming",
                   "Spin Master", "Mattel Games", "UNO", "Hot Wheels", "Pokemon", "Funko", "LEGO", "Bluey", "Paw Patrol"],
    "home": ["OXO", "Rubbermaid", "Pyrex", "Anchor Hocking", "Joseph Joseph", "Hefty", "Glad", "Ziploc", "Libbey",
             "Mainstays", "Room Essentials", "Threshold", "Better Homes & Gardens", "Sterilite", "Command"],
    "office_school": ["Sharpie", "Expo", "Post-it", "Five Star", "Mead", "Elmer's", "Scotch", "Paper Mate", "BIC", "Pilot"],
    "pet": ["Milk-Bone", "Greenies", "Temptations", "Purina", "Blue Buffalo", "Nylabone", "KONG", "Pup-Peroni"],
    "beauty_health": ["e.l.f.", "Burt's Bees", "Aquaphor", "Vaseline", "Dove", "Olay", "Native", "Dr Teal's", "Cetaphil"],
}


def search(keywords: str, brand: str | None = None, pages: int = 2) -> list[dict]:
    """Catalog search -> [{asin, title, brand, category, image_url, sales_rank}] (up to 20 per page)."""
    out, token = [], None
    for _ in range(pages):
        params = {"keywords": keywords, "marketplaceIds": spapi.MARKETPLACE_US,
                  "includedData": "summaries,salesRanks,images,identifiers,dimensions", "pageSize": 20}
        if brand:
            params["brandNames"] = brand
        if token:
            params["pageToken"] = token
        data = spapi._call("GET", "/catalog/2022-04-01/items", params)
        for item in data.get("items") or []:
            product, rank, _ = spapi.parse_catalog_item(item)
            out.append({"asin": item["asin"], **product, "sales_rank": rank})
        token = (data.get("pagination") or {}).get("nextToken")
        time.sleep(0.6)            # catalog search allows ~2 requests/second
        if not token:
            break
    return out


def run(conn, groups: list[str] | None = None, brands: list[str] | None = None, keywords: list[str] | None = None,
        pages: int = 2, max_rank: int | None = 200_000, tag: str | None = None) -> dict:
    """Add catalog products to the database. Skips items with no sales rank or a very poor one (dead listings)."""
    queries: list[tuple[str, str | None, str]] = []
    for b in brands or []:
        queries.append((b, b, f"brand:{b}"))
    for k in keywords or []:
        queries.append((k, None, tag or f"search:{k}"))
    for g in groups or ([] if (brands or keywords) else list(SEEDS)):
        for b in SEEDS.get(g, []):
            queries.append((b, b, f"discover:{g}"))
    added = seen = skipped = 0
    for kw, brand, origin in queries:
        try:
            items = search(kw, brand, pages)
        except spapi.SPAPIError as e:
            print(f"  discover {kw}: {str(e)[:100]}", flush=True)
            continue
        for it in items:
            seen += 1
            if max_rank and (not it["sales_rank"] or it["sales_rank"] > max_rank):
                skipped += 1
                continue
            new = conn.execute("SELECT 1 FROM products WHERE asin = ?", (it["asin"],)).fetchone() is None
            db.upsert_product(conn, it["asin"], title=it["title"], brand=it["brand"], category=it["category"],
                              image_url=it["image_url"], upc=it.get("upc"), weight_lb=it.get("weight_lb"), origin=origin)
            added += new
        conn.commit()
    return {"queries": len(queries), "seen": seen, "added": added, "skipped_slow_or_unranked": skipped}


# ---------------------------------------------------------------- snowball

SNOW_KEY = "snowball_brands"
SNOW_EVERY_DAYS = 14


def snowball_targets(conn, limit: int = 8) -> list[str]:
    """Brands worth pulling the whole catalog for: they already have a product you can sell (or are one approval
    away from) with real demand and a workable price, and haven't been expanded in the last two weeks."""
    import json
    from . import engine
    done = json.loads(db.get_setting(conn, SNOW_KEY) or "{}")
    score: dict[str, int] = {}
    for (asin,) in conn.execute("SELECT asin FROM products WHERE status != 'dead' AND brand IS NOT NULL AND brand != ''"):
        v = engine.list_view(conn, asin)
        s, e = v["snapshot"] or {}, v["economics"]
        if v["restricted"] or v["gated"] not in ("ungated", "approval"):
            continue
        if not s.get("sales_rank") or s["sales_rank"] > 50_000 or (e.get("sale_price") or 0) < 15 or s.get("amazon_price"):
            continue
        if (v.get("enrichment") or {}).get("sold_in_stores") is False:
            continue
        score[v["brand"]] = score.get(v["brand"], 0) + (2 if v["gated"] == "ungated" else 1)
    fresh = [b for b in score if not done.get(b) or _age_days(done[b]) >= SNOW_EVERY_DAYS]
    return sorted(fresh, key=lambda b: -score[b])[:limit]


def _age_days(iso: str) -> float:
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 86400


def snowball(conn, limit: int = 8) -> dict:
    """Find a winner, then pull the rest of that brand's catalog: the 'one diamond leads to the next' loop."""
    import json
    brands = snowball_targets(conn, limit)
    if not brands:
        return {"brands": [], "added": 0}
    r = run(conn, brands=brands, pages=3)
    done = json.loads(db.get_setting(conn, SNOW_KEY) or "{}")
    for b in brands:
        done[b] = db.now()
    db.set_setting(conn, SNOW_KEY, json.dumps(done))
    conn.commit()
    return {"brands": brands, **r}


def pull_seller(conn, seller_id: str, pages: int = 3) -> dict:
    """Add everything a seller lists (their storefront) so the pipeline can check what you can sell and source."""
    from . import stores
    items, used = stores.amazon_storefront(seller_id, pages)
    added = 0
    for it in items:
        new = conn.execute("SELECT 1 FROM products WHERE asin = ?", (it["asin"],)).fetchone() is None
        db.upsert_product(conn, it["asin"], title=it["title"], image_url=it["image_url"], origin=f"seller:{seller_id}")
        added += new
    db.add_seller(conn, seller_id)
    conn.execute("UPDATE tracked_sellers SET last_pull = ?, asin_count = ?, new_count = ? WHERE seller_id = ?",
                 (db.now(), len(items), added, seller_id))
    conn.commit()
    return {"seller": seller_id, "products": len(items), "added": added, "searches_used": used}
