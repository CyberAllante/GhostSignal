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
    last_pull  TEXT,
    status     TEXT NOT NULL DEFAULT 'active',   -- active | paused
    asin_count INTEGER DEFAULT 0,
    new_count  INTEGER DEFAULT 0                 -- ASINs that were new on the last pull
);

-- What it sells for on eBay / Facebook Marketplace (Amazon comes from snapshots).
CREATE TABLE IF NOT EXISTS channel_prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT NOT NULL REFERENCES products(asin),
    channel     TEXT NOT NULL,          -- ebay | facebook
    price       REAL NOT NULL,
    note        TEXT DEFAULT '',
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channel_prices ON channel_prices(asin, channel, id);

-- What you actually bought, where it's listed, what it sold for. One row per lot.
CREATE TABLE IF NOT EXISTS inventory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT NOT NULL REFERENCES products(asin),
    qty         INTEGER NOT NULL DEFAULT 1,
    unit_cost   REAL,
    store       TEXT DEFAULT '',
    bought_at   TEXT NOT NULL,
    channel     TEXT DEFAULT '',        -- amazon | ebay | facebook ('' = not listed yet)
    list_price  REAL,
    status      TEXT NOT NULL DEFAULT 'in_hand',   -- in_hand | listed | sold
    sold_qty    INTEGER NOT NULL DEFAULT 0,
    sold_price  REAL,                   -- per unit
    sold_at     TEXT,
    notes       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inventory_asin ON inventory(asin);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Can you sell it? status: ungated | approval | blocked. source: spapi | manual
CREATE TABLE IF NOT EXISTS eligibility (
    asin         TEXT PRIMARY KEY REFERENCES products(asin),
    status       TEXT NOT NULL,
    reason       TEXT DEFAULT '',
    approval_url TEXT DEFAULT '',
    source       TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);

-- AI enrichment (risk flags, replenishable, etc.)
CREATE TABLE IF NOT EXISTS enrichment (
    asin        TEXT PRIMARY KEY REFERENCES products(asin),
    updated_at  TEXT NOT NULL,
    data        TEXT NOT NULL       -- JSON
);

-- Every third-party seller spotted on a listing's offers, with their feedback count. Zero/low feedback = a new
-- account like yours: what they can sell, you can likely sell. Free: comes with the Seller API offers call.
CREATE TABLE IF NOT EXISTS sellers_seen (
    seller_id      TEXT NOT NULL,
    asin           TEXT NOT NULL,
    feedback_count INTEGER,
    positive_pct   REAL,
    fba            INTEGER,
    price          REAL,
    buybox         INTEGER,
    seen_at        TEXT NOT NULL,
    PRIMARY KEY (seller_id, asin)
);
CREATE INDEX IF NOT EXISTS idx_sellers_seen_fb ON sellers_seen(feedback_count);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_migrated: set[str] = set()   # schema/migration runs once per database file per process


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = Path(path or DEFAULT_DB)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)     # wait for a busy database instead of failing at once
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) == ":memory:" or str(path) not in _migrated:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()          # the migration's UPDATE opens a transaction; without this the ALTERs roll back on close
        if str(path) != ":memory:":
            _migrated.add(str(path))
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tracked_sellers)")}
    for col, ddl in (("status", "TEXT NOT NULL DEFAULT 'active'"), ("asin_count", "INTEGER DEFAULT 0"),
                     ("new_count", "INTEGER DEFAULT 0")):
        if col not in have:
            conn.execute(f"ALTER TABLE tracked_sellers ADD COLUMN {col} {ddl}")
    if "order_total" not in {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}:
        conn.execute("ALTER TABLE orders ADD COLUMN order_total REAL")   # whole-order total, repeated on each line
    # one-time: older checks called every NOT_ELIGIBLE "blocked"; only closed brands really are
    conn.execute("""UPDATE eligibility SET status = 'limited' WHERE source = 'spapi' AND status = 'blocked'
                    AND lower(COALESCE(reason, '')) NOT LIKE '%not accepting applications%'""")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    if "weight_lb" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN weight_lb REAL")     # package weight, for FBM label cost
    if "store_note" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN store_note TEXT")    # outcome of the last store-price check
    inv = {r["name"] for r in conn.execute("PRAGMA table_info(inventory)")}
    for col, ddl in (("pred_verdict", "TEXT"), ("pred_score", "INTEGER"), ("pred_profit", "REAL")):
        if col not in inv:
            conn.execute(f"ALTER TABLE inventory ADD COLUMN {col} {ddl}")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def add_seller(conn: sqlite3.Connection, seller_id: str, name: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO tracked_sellers (seller_id, name, added_at) VALUES (?, ?, ?)",
                 (seller_id, name, now()))


def upsert_product(conn: sqlite3.Connection, asin: str, **fields) -> None:
    """Insert a product or fill in fields we didn't know yet (never blank out known data)."""
    asin = asin.strip().upper()
    existing = conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()
    allowed = {"title", "brand", "category", "upc", "image_url", "status", "tags", "notes", "origin", "last_checked", "weight_lb", "store_note"}
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


GATED_STATUSES = ("ungated", "approval", "limited", "blocked")


def set_eligibility(conn: sqlite3.Connection, asin: str, status: str, source: str = "manual",
                    reason: str = "", approval_url: str = "") -> None:
    if status not in GATED_STATUSES:
        raise ValueError(f"status must be one of {GATED_STATUSES}")
    upsert_product(conn, asin)
    conn.execute(
        """INSERT OR REPLACE INTO eligibility (asin, status, reason, approval_url, source, checked_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (asin.upper(), status, reason, approval_url, source, now()),
    )


def get_eligibility(conn: sqlite3.Connection, asin: str) -> dict | None:
    row = conn.execute("SELECT * FROM eligibility WHERE asin = ?", (asin,)).fetchone()
    return dict(row) if row else None


CHANNELS = ("amazon", "ebay", "facebook")


def set_channel_price(conn: sqlite3.Connection, asin: str, channel: str, price: float, note: str = "") -> None:
    if channel not in ("ebay", "facebook"):
        raise ValueError("channel must be ebay or facebook (Amazon prices come from snapshots)")
    upsert_product(conn, asin)
    conn.execute("INSERT INTO channel_prices (asin, channel, price, note, captured_at) VALUES (?, ?, ?, ?, ?)",
                 (asin.upper(), channel, price, note, now()))


def channel_prices(conn: sqlite3.Connection, asin: str) -> dict:
    rows = conn.execute(
        """SELECT c.channel, c.price FROM channel_prices c
           JOIN (SELECT channel, MAX(id) mid FROM channel_prices WHERE asin = ? GROUP BY channel) m ON m.mid = c.id""",
        (asin,)).fetchall()
    return {r["channel"]: r["price"] for r in rows}


def new_item_id(conn: sqlite3.Connection) -> str:
    """10-char ID for things that aren't on Amazon (e.g. a Facebook find): GS + 8 digits."""
    n = conn.execute("SELECT COUNT(*) FROM products WHERE asin LIKE 'GS%'").fetchone()[0] + 1
    while conn.execute("SELECT 1 FROM products WHERE asin = ?", (f"GS{n:08d}",)).fetchone():
        n += 1
    return f"GS{n:08d}"


def add_inventory(conn: sqlite3.Connection, asin: str, qty: int, unit_cost: float | None, store: str = "",
                  channel: str = "", list_price: float | None = None, notes: str = "",
                  pred: dict | None = None) -> int:
    if channel and channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    upsert_product(conn, asin)
    cur = conn.execute(
        """INSERT INTO inventory (asin, qty, unit_cost, store, bought_at, channel, list_price, status, notes,
                                  pred_verdict, pred_score, pred_profit)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asin.upper(), int(qty), unit_cost, store, now(), channel, list_price,
         "listed" if channel and list_price else "in_hand", notes,
         (pred or {}).get("verdict"), (pred or {}).get("score"), (pred or {}).get("profit")))
    return cur.lastrowid


def update_inventory(conn: sqlite3.Connection, item_id: int, **fields) -> None:
    allowed = {"qty", "unit_cost", "store", "channel", "list_price", "status", "sold_qty", "sold_price", "sold_at", "notes"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if data.get("channel") and data["channel"] not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    if data.get("status") == "sold":
        row = conn.execute("SELECT qty FROM inventory WHERE id = ?", (item_id,)).fetchone()
        data.setdefault("sold_qty", row["qty"] if row else 0)
        data.setdefault("sold_at", now())
    elif data.get("channel") and data.get("list_price") is not None and "status" not in data:
        data["status"] = "listed"
    if data:
        conn.execute(f"UPDATE inventory SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                     [*data.values(), item_id])


def inventory_rows(conn: sqlite3.Connection, asin: str | None = None) -> list[dict]:
    q = """SELECT i.*, p.title, p.brand, p.image_url FROM inventory i JOIN products p ON p.asin = i.asin"""
    rows = conn.execute(q + (" WHERE i.asin = ?" if asin else "") + " ORDER BY i.id DESC",
                        (asin,) if asin else ()).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["profit"] = (round((d["sold_price"] - (d["unit_cost"] or 0)) * d["sold_qty"], 2)
                       if d["status"] == "sold" and d["sold_price"] is not None else None)
        out.append(d)
    return out


def archive_products(conn: sqlite3.Connection, asins: list[str], restore: bool = False) -> int:
    """Hide products from every list (status 'dead') without deleting history; restore puts them back to 'watch'."""
    asins = [a.strip().upper() for a in asins if a]
    if not asins:
        return 0
    q = ",".join("?" * len(asins))
    cur = conn.execute(f"UPDATE products SET status = ? WHERE asin IN ({q})", ["watch" if restore else "dead", *asins])
    return cur.rowcount


def outcomes(conn: sqlite3.Connection) -> dict:
    """Predicted vs actual: how the verdict at purchase time compares with what the lots really made."""
    sold = [r for r in inventory_rows(conn) if r["status"] == "sold" and r["profit"] is not None]
    by: dict[str, dict] = {}
    for r in sold:
        k = r.get("pred_verdict") or "UNSCORED"
        d = by.setdefault(k, {"lots": 0, "wins": 0, "profit": 0.0, "predicted": 0.0, "predicted_n": 0})
        d["lots"] += 1
        d["wins"] += 1 if r["profit"] > 0 else 0
        d["profit"] += r["profit"]
        if r.get("pred_profit") is not None:
            d["predicted"] += r["pred_profit"] * r["sold_qty"]
            d["predicted_n"] += 1
    for d in by.values():
        d["profit"] = round(d["profit"], 2)
        d["predicted"] = round(d["predicted"], 2)
        d["win_rate"] = round(d["wins"] / d["lots"], 2) if d["lots"] else None
    return {"sold_lots": len(sold), "by_verdict": by,
            "total_profit": round(sum(r["profit"] for r in sold), 2)}


def spend_by_buyer(conn: sqlite3.Connection) -> dict:
    """Total Amazon spend per buyer, counting each order once (its total is repeated on every line)."""
    out = {}
    for r in conn.execute("""SELECT buyer, COUNT(*) AS orders, ROUND(SUM(t), 2) AS spend, SUM(t IS NULL) AS unknown FROM (
                               SELECT buyer, order_id, MAX(order_total) AS t FROM orders GROUP BY buyer, order_id) GROUP BY buyer"""):
        out[r["buyer"]] = {"orders": r["orders"], "spend": r["spend"] or 0.0, "orders_without_total": r["unknown"]}
    return out


AMAZON_SELLER_IDS = {"A2R2RITDJNW1Q6", "ATVPDKIKX0DER"}


def record_sellers(conn: sqlite3.Connection, asin: str, offers_payload: dict) -> int:
    """Remember who is selling on this listing and how much feedback they have."""
    n = 0
    for o in offers_payload.get("Offers") or []:
        sid = o.get("SellerId")
        if not sid:
            continue
        fb = o.get("SellerFeedbackRating") or {}
        price = (o.get("ListingPrice") or {}).get("Amount")
        conn.execute("""INSERT OR REPLACE INTO sellers_seen (seller_id, asin, feedback_count, positive_pct, fba, price, buybox, seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (sid, asin, fb.get("FeedbackCount"), fb.get("SellerPositiveFeedbackRating"),
                      1 if o.get("IsFulfilledByAmazon") else 0, float(price) if price is not None else None,
                      1 if o.get("IsBuyBoxWinner") else 0, now()))
        n += 1
    return n


def found_sellers(conn: sqlite3.Connection, limit: int = 40, max_feedback: int = 200) -> list[dict]:
    """Third-party sellers seen on your listings, newest accounts first (lowest feedback), with how many of the
    listings they sit on you can sell yourself."""
    marks = ",".join("?" * len(AMAZON_SELLER_IDS))
    rows = conn.execute(f"""
        SELECT s.seller_id, MIN(s.feedback_count) AS feedback, MAX(s.positive_pct) AS positive,
               COUNT(DISTINCT s.asin) AS listings, SUM(s.fba) AS fba_listings,
               SUM(CASE WHEN e.status = 'ungated' THEN 1 ELSE 0 END) AS sellable, MAX(s.seen_at) AS last_seen,
               (SELECT COUNT(*) FROM products p WHERE p.origin LIKE '%seller:' || s.seller_id || '%') AS pulled
        FROM sellers_seen s LEFT JOIN eligibility e ON e.asin = s.asin
        WHERE s.seller_id NOT IN ({marks}) AND COALESCE(s.feedback_count, 0) <= ?
        GROUP BY s.seller_id
        ORDER BY feedback ASC, sellable DESC, listings DESC LIMIT ?""", [*sorted(AMAZON_SELLER_IDS), max_feedback, limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["examples"] = [x[0] for x in conn.execute(
            """SELECT p.title FROM sellers_seen s JOIN products p ON p.asin = s.asin
               WHERE s.seller_id = ? AND p.title IS NOT NULL LIMIT 2""", (d["seller_id"],))]
        out.append(d)
    return out
