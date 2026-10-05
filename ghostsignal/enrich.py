"""AI analyst: Claude reads product titles/brands and flags sourcing risks.

Requires `pip install anthropic` and ANTHROPIC_API_KEY (or `ant auth login`).
Output is a structured JSON verdict per product, stored in the `enrichment` table
and folded into the opportunity score as penalties/bonuses.
"""

from __future__ import annotations

import json

from . import db

MODEL = "claude-opus-5-5"
BATCH = 25

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "asin": {"type": "string"},
        "product_type": {"type": "string"},
        "replenishable": {"type": "boolean"},
        "gating_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "hazmat_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "ip_complaint_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "expiration_dated": {"type": "boolean"},
        "likely_retailers": {"type": "array", "items": {"type": "string"}},
        "bundle_or_multipack": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["asin", "product_type", "replenishable", "gating_risk", "hazmat_risk",
                 "ip_complaint_risk", "expiration_dated", "likely_retailers", "bundle_or_multipack", "notes"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {"products": {"type": "array", "items": ITEM_SCHEMA}},
    "required": ["products"],
    "additionalProperties": False,
}

SYSTEM = """You are a sourcing analyst for an Amazon/eBay online & retail arbitrage seller in the US.
For each product, judge from the title, brand and category:
- replenishable: consumable people re-buy (food, toiletries, filters, pet supplies).
- gating_risk: chance a new seller is category- or brand-restricted (e.g. many grocery brands, beauty, Disney, Nike, Apple → high).
- hazmat_risk: aerosols, batteries, flammables, some cosmetics/supplements.
- ip_complaint_risk: brands known to file IP complaints against resellers.
- expiration_dated: Amazon requires expiration-date handling (food, supplements, cosmetics).
- likely_retailers: US physical/online stores most likely to stock it, as short keys from:
  walmart, target, costco, samsclub, homedepot, lowes, bestbuy, cvs, walgreens, traderjoes, wholefoods, kohls, dollartree.
- bundle_or_multipack: the Amazon listing is a multi-pack or bundle of a single retail unit.
- notes: one short sentence on the biggest risk or sourcing tip.
Be calibrated: use "medium" when unsure. Return one entry per input ASIN."""


def enrich(conn, asins: list[str] | None = None, force: bool = False) -> int:
    import anthropic  # optional dependency

    client = anthropic.Anthropic()
    if asins is None:
        q = "SELECT p.asin FROM products p LEFT JOIN enrichment e ON e.asin = p.asin WHERE p.title IS NOT NULL"
        if not force:
            q += " AND e.asin IS NULL"
        asins = [r[0] for r in conn.execute(q)]
    done = 0
    for i in range(0, len(asins), BATCH):
        rows = conn.execute(
            f"SELECT asin, title, brand, category FROM products WHERE asin IN ({','.join('?' * len(asins[i:i + BATCH]))})",
            asins[i:i + BATCH],
        ).fetchall()
        items = [dict(r) for r in rows if r["title"]]
        if not items:
            continue
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": json.dumps(items)}],
        )
        if response.stop_reason == "refusal":
            print(f"  batch {i // BATCH + 1}: declined, skipping")
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        for item in json.loads(text).get("products", []):
            if item.get("asin") in asins:
                conn.execute(
                    "INSERT OR REPLACE INTO enrichment (asin, updated_at, data) VALUES (?, ?, ?)",
                    (item["asin"], db.now(), json.dumps(item)),
                )
                done += 1
        conn.commit()
    return done
