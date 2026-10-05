"""SQLite storage. One file, zero setup — easy to move to Postgres/Supabase later.

Design: products are the spine. Everything else is a time-stamped observation
hanging off an ASIN, so history is never overwritten and we can detect change.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("GHOSTSIGNAL_DB", Path(__file__).resolve().parent.parent / "data" / "ghostsignal.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    asin         TEXT PRIMARY KEY,
    title        TEXT,
    brand        TEXT,
    category     TEXT,
    upc          TEXT,
    image_url    TEXT,
    status       TEXT NOT NULL DEFAULT 'watch',   -- watch | buy | research | pass | dead
    tags         TEXT DEFAULT '',
    notes        TEXT DEFAULT '',
    origin       TEXT DEFAULT '',                 -- how we found it: orders, stealthseller, manual, seller:<id>
    first_seen   TEXT NOT NULL,
    last_checked TEXT
);

-- Marketplace state at a point in time (Keepa, Stealth Seller export, manual entry).
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asin            TEXT NOT NULL REFERENCES products(asin),
    captured_at     TEXT NOT NULL,
    source          TEXT NOT NULL,
    buy_box         REAL,
    amazon_price    REAL,       -- Amazon itself as a seller (null = not on listing)
    avg_price_90    REAL,
    sales_rank      INTEGER,
    monthly_sold    INTEGER,
    offer_count     INTEGER,
    fba_offers      INTEGER,
    fbm_offers      INTEGER,
    referral_pct    REAL,       -- e.g. 0.15
    fba_fee         REAL,       -- pick & pack, dollars
    ebay_sold_price REAL,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_asin ON snapshots(asin, captured_at);

-- Where you can buy it, and for how much.
CREATE TABLE IF NOT EXISTS retail_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT NOT NULL REFERENCES products(asin),
    retailer    TEXT NOT NULL,
    price       REAL,
    pack_qty    INTEGER NOT NULL DEFAULT 1,   -- retail units needed per Amazon unit
    promo       TEXT DEFAULT '',              -- "B2G1", "20% off w/ circle", etc.
    in_stock    INTEGER,                      -- 1/0/null(unknown)
    url         TEXT DEFAULT '',
    captured_at TEXT NOT NULL,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sources_asin ON retail_sources(asin, captured_at);

-- Purchase-intent data: real orders from people who opted in.
-- No names/addresses stored — `buyer` is a label you choose ("me", "gf", "p03").
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer       TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    order_date  TEXT,
    asin        TEXT NOT NULL,
    title       TEXT,
    unit_price  REAL,
    quantity    INTEGER DEFAULT 1,
    source_file TEXT,
    UNIQUE(buyer, order_id, asin)
);
CREATE INDEX IF NOT EXISTS idx_orders_asin ON orders(asin);

-- Every scoring run writes a signal row, so we can see a product flip PASS -> BUY.
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT NOT NULL REFERENCES products(asin),
    created_at  TEXT NOT NULL,
    score       INTEGER NOT NULL,
    verdict     TEXT NOT NULL,       -- BUY | RESEARCH | PASS
    profit      REAL,
    roi         REAL,
    best_source TEXT,
    reasons     TEXT,                -- JSON list
    alerted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_asin ON signals(asin, created_at);

-- Sellers you shadow (Stealth Seller-style storefront tracking).
CREATE TABLE IF NOT EXISTS tracked_sellers (
    seller_id  TEXT PRIMARY KEY,
    name       TEXT,
    added_at   TEXT NOT NULL,
    last_pull  TEXT
);

-- AI enrichment (risk flags, replenishable, etc.)
CREATE TABLE IF NOT EXISTS enrichment (
    asin        TEXT PRIMARY KEY REFERENCES products(asin),
    updated_at  TEXT NOT NULL,
    data        TEXT NOT NULL       -- JSON
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = Path(path or DEFAULT_DB)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_product(conn: sqlite3.Connection, asin: str, **fields) -> None:
    """Insert a product or fill in fields we didn't know yet (never blank out known data)."""
    asin = asin.strip().upper()
    existing = conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()
    allowed = {"title", "brand", "category", "upc", "image_url", "status", "tags", "notes", "origin", "last_checked"}
    fields = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    if existing is None:
        fields.setdefault("first_seen", now())
        cols = ["asin", *fields.keys()]
        conn.execute(
            f"INSERT INTO products ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [asin, *fields.values()],
        )
        return
    updates = {}
    for k, v in fields.items():
        if k == "origin" and existing["origin"]:
            if v not in existing["origin"].split(","):
                updates[k] = f"{existing['origin']},{v}"
        elif k in ("status", "last_checked") or not existing[k]:
            updates[k] = v
    if updates:
        conn.execute(
            f"UPDATE products SET {', '.join(f'{k} = ?' for k in updates)} WHERE asin = ?",
            [*updates.values(), asin],
        )


def add_snapshot(conn: sqlite3.Connection, asin: str, source: str, **fields) -> None:
    cols = {
        "buy_box", "amazon_price", "avg_price_90", "sales_rank", "monthly_sold", "offer_count",
        "fba_offers", "fbm_offers", "referral_pct", "fba_fee", "ebay_sold_price", "raw_json",
    }
    data = {k: v for k, v in fields.items() if k in cols and v is not None}
    data.update(asin=asin.upper(), source=source, captured_at=fields.get("captured_at") or now())
    conn.execute(
        f"INSERT INTO snapshots ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
        list(data.values()),
    )
    conn.execute("UPDATE products SET last_checked = ? WHERE asin = ?", (data["captured_at"], data["asin"]))


def add_source(conn: sqlite3.Connection, asin: str, retailer: str, price: float | None, **fields) -> None:
    data = {
        "asin": asin.upper(),
        "retailer": retailer.lower().strip(),
        "price": price,
        "pack_qty": int(fields.get("pack_qty") or 1),
        "promo": fields.get("promo") or "",
        "in_stock": fields.get("in_stock"),
        "url": fields.get("url") or "",
        "note": fields.get("note") or "",
        "captured_at": fields.get("captured_at") or now(),
    }
    conn.execute(
        f"INSERT INTO retail_sources ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
        list(data.values()),
    )


def latest_snapshot(conn: sqlite3.Connection, asin: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE asin = ? ORDER BY captured_at DESC, id DESC LIMIT 1", (asin,)
    ).fetchone()


def latest_sources(conn: sqlite3.Connection, asin: str) -> list[sqlite3.Row]:
    """Most recent observation per retailer."""
    return conn.execute(
        """
        SELECT s.* FROM retail_sources s
        JOIN (SELECT retailer, MAX(id) AS mid FROM retail_sources WHERE asin = ? GROUP BY retailer) m
          ON s.id = m.mid
        ORDER BY s.price IS NULL, s.price / s.pack_qty
        """,
        (asin,),
    ).fetchall()


def order_stats(conn: sqlite3.Connection, asin: str) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS orders, COALESCE(SUM(quantity), 0) AS units,
               COUNT(DISTINCT buyer) AS buyers, MAX(order_date) AS last_order,
               AVG(unit_price) AS avg_paid
        FROM orders WHERE asin = ?
        """,
        (asin,),
    ).fetchone()
    repeat = conn.execute(
        "SELECT COUNT(*) FROM (SELECT buyer FROM orders WHERE asin = ? GROUP BY buyer HAVING COUNT(*) > 1)",
        (asin,),
    ).fetchone()[0]
    return {**dict(row), "repeat_buyers": repeat}


def last_signal(conn: sqlite3.Connection, asin: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM signals WHERE asin = ? ORDER BY created_at DESC, id DESC LIMIT 1", (asin,)
    ).fetchone()
