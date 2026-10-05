"""Get data in: Amazon order history (3 formats) and product lists from any tool.

Amazon order formats supported:
  1. GhostSignal browser scraper output (tools/amazon_orders_scraper.js) — JSON or CSV
  2. Amazon "Request Your Data" privacy export — Retail.OrderHistory.*.csv
  3. Legacy Amazon "Order History Reports" items CSV (ASIN/ISBN column)

Product lists: any CSV with an ASIN column (Stealth Seller, Keepa, SellerAmp,
spreadsheets). Columns are matched by alias, so exports with slightly different
headers still work.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path

from . import db

ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8}|\d{9}[\dX])\b")


# ---------- parsing helpers ----------

def norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def money(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def number(v) -> int | None:
    """'21.2K' -> 21200, '<50' -> 25, '1,234' -> 1234, '-' -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").upper()
    m = re.search(r"(\d+(?:\.\d+)?)\s*([KM]?)", s)
    if not m:
        return None
    n = float(m.group(1)) * {"": 1, "K": 1_000, "M": 1_000_000}[m.group(2)]
    if s.startswith("<"):
        n /= 2
    return int(n)


def date(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y",
                "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s


def pick(row: dict, *aliases: str):
    """Return the first non-empty value whose normalized header matches an alias."""
    keys = {norm(k): k for k in row}
    for a in aliases:
        k = keys.get(norm(a))
        if k is not None and str(row[k]).strip() not in ("", "-", "N/A", "Not Applicable"):
            return row[k]
    return None


def read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        return data.get("orders", data) if isinstance(data, dict) else data
    return list(csv.DictReader(io.StringIO(text)))


# ---------- Amazon orders ----------

def import_orders(conn, path: str | Path, buyer: str) -> dict:
    """Import one person's Amazon orders. Only product-level fields are kept —
    addresses, payment and gift details in the privacy export are dropped."""
    path = Path(path)
    rows = read_rows(path)
    added = skipped = 0
    for r in rows:
        asin = pick(r, "asin", "ASIN/ISBN", "ASIN")
        if not asin or not ASIN_RE.fullmatch(str(asin).strip().upper()):
            skipped += 1
            continue
        asin = str(asin).strip().upper()
        status = (pick(r, "Order Status") or "").lower()
        if "cancel" in status:
            skipped += 1
            continue
        title = pick(r, "title", "Product Name", "Title")
        order_id = pick(r, "order_id", "Order ID") or f"unknown-{asin}"
        qty = number(pick(r, "quantity", "Quantity", "Original Quantity")) or 1
        unit = money(pick(r, "unit_price", "price", "Unit Price", "Purchase Price Per Unit"))
        cur = conn.execute(
            """INSERT OR IGNORE INTO orders (buyer, order_id, order_date, asin, title, unit_price, quantity, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (buyer, str(order_id), date(pick(r, "order_date", "Order Date")), asin, title, unit, qty, path.name),
        )
        total = money(pick(r, "order_total"))
        if total is not None:    # fill in on re-imports too, so older uploads gain their totals
            conn.execute("UPDATE orders SET order_total = ? WHERE buyer = ? AND order_id = ? AND asin = ? AND order_total IS NULL",
                         (total, buyer, str(order_id), asin))
        if cur.rowcount:
            added += 1
            db.upsert_product(conn, asin, title=title, origin="orders",
                              category=pick(r, "category", "Category"))
        else:
            skipped += 1
    conn.commit()
    return {"rows": len(rows), "added": added, "skipped": skipped}


# ---------- product lists (Stealth Seller / Keepa / any CSV) ----------

def import_products(conn, path: str | Path, source: str = "csv") -> dict:
    """Import a product list. If price/rank columns exist they become a snapshot;
    if cost/retailer columns exist they become a retail source."""
    path = Path(path)
    rows = read_rows(path)
    n = snaps = srcs = 0
    for r in rows:
        raw_asin = pick(r, "asin", "ASIN", "Product ASIN", "asin1")
        if not raw_asin:
            # Fall back to scanning the row (e.g. an Amazon URL column)
            m = ASIN_RE.search(" ".join(str(v) for v in r.values()))
            raw_asin = m.group(1) if m else None
        if not raw_asin:
            continue
        asin = str(raw_asin).strip().upper()
        n += 1
        db.upsert_product(
            conn, asin,
            title=pick(r, "title", "Title", "Product Name", "name"),
            brand=pick(r, "brand", "Brand", "Manufacturer"),
            category=pick(r, "category", "Category", "Root Category", "Categories: Root"),
            upc=pick(r, "upc", "UPC", "Product Codes: UPC", "EAN"),
            image_url=pick(r, "image", "Image", "image_url", "Image URL"),
            origin=source,
        )
        snap = dict(
            buy_box=money(pick(r, "Buy Box", "buy_box", "Buy Box: Current", "Buy Box 🚚: Current", "Price", "Sale price")),
            amazon_price=money(pick(r, "Amazon", "Amazon: Current", "amazon_price")),
            avg_price_90=money(pick(r, "Avg Price", "avg_price", "Buy Box: 90 days avg.", "Buy Box 🚚: 90 days avg.")),
            sales_rank=number(pick(r, "Sales Rank", "sales_rank", "BSR", "Sales Rank: Current")),
            monthly_sold=number(pick(r, "Monthly Sold", "monthly_sold", "Monthly Sales", "Bought in past month")),
            offer_count=number(pick(r, "Offers", "offer_count", "New Offer Count: Current", "Total Offer Count")),
            fba_offers=number(pick(r, "FBA Offers", "fba_offers")),
            fbm_offers=number(pick(r, "FBM Offers", "fbm_offers")),
            fba_fee=money(pick(r, "FBA Fee", "fba_fee", "FBA Pick&Pack Fee")),
            ebay_sold_price=money(pick(r, "eBay Price", "ebay_price", "ebay_sold_price")),
        )
        if any(v is not None for v in snap.values()):
            db.add_snapshot(conn, asin, source, **snap)
            snaps += 1
        cost = money(pick(r, "Cost", "cost", "Cost price", "Source Price", "Buy Cost"))
        retailer = pick(r, "Retailer", "Store", "Source", "source_store")
        url = pick(r, "Source URL", "source_url", "Store URL", "URL")
        if cost or retailer:
            if not retailer and url:
                m = re.search(r"https?://(?:www\.)?([^/]+)", str(url))
                retailer = m.group(1).split(".")[0] if m else "unknown"
            db.add_source(conn, asin, str(retailer or "unknown"), cost, url=url,
                          promo=pick(r, "Promo", "promo", "Deal"))
            srcs += 1
    conn.commit()
    return {"rows": len(rows), "products": n, "snapshots": snaps, "sources": srcs}


def import_asin_text(conn, text: str, source: str = "manual") -> int:
    """Pull every ASIN out of arbitrary pasted text (URLs, lists, notes)."""
    found = list(dict.fromkeys(m.group(1) for m in ASIN_RE.finditer(text.upper())))
    for asin in found:
        db.upsert_product(conn, asin, origin=source)
    conn.commit()
    return len(found)


def parse_seller_id(text: str) -> str | None:
    """Seller ID from a raw ID or any Amazon storefront / seller profile URL."""
    t = (text or "").strip()
    m = re.search(r"[?&](?:seller|me|sellerID|merchant)=([A-Z0-9]{10,20})", t, re.I)
    if m:
        return m.group(1).upper()
    m = re.fullmatch(r"A[A-Z0-9]{9,19}", t.upper())
    return m.group(0) if m else None
