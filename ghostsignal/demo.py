"""Sample data so the dashboard isn't empty on first run.

Numbers are illustrative (loosely based on public listings) — not live prices.
Wipe with: rm data/ghostsignal.db
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db

PRODUCTS = [
    dict(asin="B06W9N8X9H", title="Trader Joe's Everything but the Bagel Sesame Seasoning Blend 2.3 oz, Pack of 1",
         brand="Trader Joe's", category="Grocery & Gourmet Food",
         snap=dict(buy_box=6.75, avg_price_90=6.27, sales_rank=7384, monthly_sold=2000, offer_count=41,
                   amazon_price=6.75, fba_fee=3.22),
         sources=[("traderjoes", 1.99, 1, "", 1), ("walmart", 8.50, 1, "", None)]),
    dict(asin="B00NLVM6WK", title="Wild Planet Wild Albacore Tuna Cans, 5 Ounce, 4 Pack",
         brand="Wild Planet", category="Grocery & Gourmet Food",
         snap=dict(buy_box=23.99, avg_price_90=16.13, sales_rank=2567, monthly_sold=2000, offer_count=18,
                   amazon_price=23.99, fba_fee=4.10),
         sources=[("costco", 20.99, 1, "", 1), ("wholefoods", 18.79, 1, "", None)]),
    dict(asin="B00BCK64FC", title="Trader Joe's Himalayan Pink Salt Crystals with Built in Grinder 4.5 Oz, (2-Pack)",
         brand="Trader Joe's", category="Grocery & Gourmet Food",
         snap=dict(buy_box=12.98, avg_price_90=12.77, sales_rank=82100, monthly_sold=40, offer_count=13,
                   fba_fee=3.86, ebay_sold_price=15.50),
         sources=[("traderjoes", 1.99, 2, "was $3.88", 1), ("walmart", 19.99, 1, "", None)]),
    dict(asin="B08YMHF438", title="THE SNAK YARD SHIITAKE MUSHROOM (10.6 OZ BAG)",
         brand="The Snak Yard", category="Grocery & Gourmet Food",
         snap=dict(buy_box=23.88, avg_price_90=24.85, monthly_sold=40, offer_count=11, fba_fee=4.40),
         sources=[]),
    dict(asin="B00F0FC3OC", title="Pocky Cream Covered Biscuit Sticks, Strawberry, 1.41 Ounce (Pack of 10)",
         brand="Pocky", category="Grocery & Gourmet Food",
         snap=dict(buy_box=30.89, avg_price_90=29.40, sales_rank=20200, monthly_sold=100, offer_count=10,
                   fba_fee=4.95),
         sources=[("costco", 10.99, 1, "10-ct box", 1), ("samsclub", 11.57, 1, "", None), ("target", 2.79, 10, "", 1)]),
]

# Fake opted-in network orders: (buyer, asin, days_ago, qty)
ORDERS = [
    ("me", "B06W9N8X9H", 12, 2), ("me", "B06W9N8X9H", 70, 1), ("gf", "B06W9N8X9H", 30, 1),
    ("mom", "B06W9N8X9H", 45, 3), ("p04", "B06W9N8X9H", 8, 1),
    ("gf", "B00F0FC3OC", 5, 1), ("gf", "B00F0FC3OC", 40, 1), ("p05", "B00F0FC3OC", 20, 1), ("me", "B00F0FC3OC", 2, 1),
    ("mom", "B00NLVM6WK", 15, 1), ("p04", "B00NLVM6WK", 60, 2),
]

ENRICH = {
    "B06W9N8X9H": dict(product_type="seasoning", replenishable=True, gating_risk="medium", hazmat_risk="low",
                       ip_complaint_risk="low", expiration_dated=True, likely_retailers=["traderjoes"],
                       bundle_or_multipack=False, notes="Private label only sold at Trader Joe's; Amazon on listing."),
    "B00F0FC3OC": dict(product_type="snack", replenishable=True, gating_risk="medium", hazmat_risk="low",
                       ip_complaint_risk="low", expiration_dated=True, likely_retailers=["costco", "samsclub", "target"],
                       bundle_or_multipack=True, notes="Club-store multipacks are the cheapest route; watch best-by dates."),
}


def seed(conn) -> None:
    for p in PRODUCTS:
        db.upsert_product(conn, p["asin"], title=p["title"], brand=p["brand"], category=p["category"], origin="demo")
        db.add_snapshot(conn, p["asin"], "demo", **p["snap"])
        for retailer, price, pack, promo, stock in p["sources"]:
            db.add_source(conn, p["asin"], retailer, price, pack_qty=pack, promo=promo, in_stock=stock)
    today = datetime.now(timezone.utc).date()
    for i, (buyer, asin, ago, qty) in enumerate(ORDERS):
        conn.execute(
            "INSERT OR IGNORE INTO orders (buyer, order_id, order_date, asin, quantity, source_file) VALUES (?,?,?,?,?,?)",
            (buyer, f"demo-{i}", (today - timedelta(days=ago)).isoformat(), asin, qty, "demo"),
        )
    # A few things already bought, on different channels.
    if not conn.execute("SELECT 1 FROM inventory").fetchone():
        db.add_inventory(conn, "B00F0FC3OC", 6, 10.99, "costco", "amazon", 30.89)
        sold = db.add_inventory(conn, "B00F0FC3OC", 2, 10.99, "costco", "facebook", 22.00)
        db.update_inventory(conn, sold, status="sold", sold_price=22.00)
        db.set_channel_price(conn, "B00F0FC3OC", "facebook", 22.00)
        db.add_inventory(conn, "B00BCK64FC", 4, 3.98, "traderjoes")
    for asin, data in ENRICH.items():
        conn.execute("INSERT OR REPLACE INTO enrichment (asin, updated_at, data) VALUES (?,?,?)",
                     (asin, db.now(), json.dumps({"asin": asin, **data})))
    conn.commit()


def has_demo(conn) -> bool:
    return conn.execute("SELECT 1 FROM products WHERE origin = 'demo' LIMIT 1").fetchone() is not None


def clear_demo(conn) -> int:
    """Delete only the sample data from `gs demo`. Anything you imported or added is untouched."""
    asins = [r[0] for r in conn.execute("SELECT asin FROM products WHERE origin = 'demo'")]
    for table in ("snapshots", "retail_sources", "signals", "enrichment", "eligibility", "channel_prices", "inventory"):
        conn.executemany(f"DELETE FROM {table} WHERE asin = ?", [(a,) for a in asins])
    conn.execute("DELETE FROM orders WHERE source_file = 'demo'")
    conn.executemany("DELETE FROM products WHERE asin = ?", [(a,) for a in asins])
    conn.commit()
    return len(asins)
