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
                  "includedData": "summaries,salesRanks,images", "pageSize": 20}
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
        pages: int = 2, max_rank: int | None = 200_000) -> dict:
    """Add catalog products to the database. Skips items with no sales rank or a very poor one (dead listings)."""
    queries: list[tuple[str, str | None, str]] = []
    for b in brands or []:
        queries.append((b, b, f"brand:{b}"))
    for k in keywords or []:
        queries.append((k, None, f"search:{k}"))
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
                              image_url=it["image_url"], origin=origin)
            added += new
        conn.commit()
    return {"queries": len(queries), "seen": seen, "added": added, "skipped_slow_or_unranked": skipped}
