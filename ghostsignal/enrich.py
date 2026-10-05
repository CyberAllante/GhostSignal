"""AI analyst: Claude reads product titles/brands and flags sourcing risks.

Needs ANTHROPIC_API_KEY. Talks to the Messages API over plain HTTPS, no SDK to install.
Output is a structured JSON verdict per product, stored in the `enrichment` table
and folded into the opportunity score as penalties/bonuses.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import db

MODEL = "claude-haiku-4-5-20251001"   # classification only, so the cheap fast model is plenty
API = "https://api.anthropic.com/v1/messages"
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
        "sold_in_stores": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["asin", "product_type", "replenishable", "gating_risk", "hazmat_risk",
                 "ip_complaint_risk", "expiration_dated", "likely_retailers", "bundle_or_multipack", "sold_in_stores", "notes"],
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
- sold_in_stores: true if this brand/product is a real brand that physical or major online retailers (Walmart, Target,
  Costco, Home Depot, Best Buy, CVS, grocery chains...) commonly stock. FALSE for brands that look Amazon-only:
  invented or unfamiliar names on generic factory-made goods (apparel, phone cases, gadgets, home accessories),
  private-label brands sold mainly through Amazon, or items with no recognisable retail presence. When unsure, use true.
  likely_retailers must be [] whenever sold_in_stores is false.
- bundle_or_multipack: the Amazon listing is a multi-pack or bundle of a single retail unit.
- notes: one short sentence on the biggest risk or sourcing tip.
Be calibrated: use "medium" when unsure. Return one entry per input ASIN."""


def _ask(items: list[dict]) -> list[dict]:
    """One batch through the Messages API (plain HTTPS, no SDK needed). Returns the per-product verdicts."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    prompt = ("Analyse these products. Return ONLY a JSON object {\"products\": [...]} with one entry per input, "
              "each following this JSON schema: " + json.dumps(ITEM_SCHEMA) + "\n\nProducts:\n" + json.dumps(items))
    body = json.dumps({"model": MODEL, "max_tokens": 8000, "system": SYSTEM,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for attempt in range(4):
        req = urllib.request.Request(API, data=body, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"Anthropic API {e.code}: {e.read()[:300]!r}") from e
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]).get("products", [])


def enrich(conn, asins: list[str] | None = None, force: bool = False) -> int:
    done = 0
    for i in range(0, len(asins), BATCH):
        rows = conn.execute(
            f"SELECT asin, title, brand, category FROM products WHERE asin IN ({','.join('?' * len(asins[i:i + BATCH]))})",
            asins[i:i + BATCH],
        ).fetchall()
        items = [dict(r) for r in rows if r["title"]]
        if not items:
            continue
        try:
            results = _ask(items)
        except (RuntimeError, ValueError, json.JSONDecodeError) as e:
            print(f"  batch {i // BATCH + 1}: skipped ({str(e)[:160]})", flush=True)
            if str(e).startswith(("Anthropic API 400", "Anthropic API 401", "Anthropic API 403")):
                break    # a key/config problem, not a one-off: stop instead of failing every batch
            continue
        for item in results:
            if item.get("asin") in asins:
                if item.get("sold_in_stores") is False:
                    item["likely_retailers"] = []
                conn.execute(
                    "INSERT OR REPLACE INTO enrichment (asin, updated_at, data) VALUES (?, ?, ?)",
                    (item["asin"], db.now(), json.dumps(item)),
                )
                done += 1
        conn.commit()
    return done
