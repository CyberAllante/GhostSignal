"""Ungating opportunities: which approvals would unlock the most good products.

Amazon's restriction reasons name what you need ("approval to list in the Grocery & Gourmet Foods category",
"approval to list in this brand"). One category approval unlocks every product in it; a brand approval unlocks
that brand. We group the gated products by those requirements and rank them by how many *strong* products
(real demand, decent price) each approval would open up.
"""

from __future__ import annotations

import re
import statistics

from . import engine

CAT_RE = re.compile(r"approval to list in the (.+?) category", re.I)
STRONG_RANK, STRONG_PRICE = 50_000, 10.0


def _strong(v: dict) -> bool:
    s, e = v["snapshot"] or {}, v["economics"]
    return bool(s.get("sales_rank") and s["sales_rank"] <= STRONG_RANK and (e.get("sale_price") or 0) >= STRONG_PRICE)


def targets(conn) -> dict:
    cats: dict[str, list] = {}
    brands: dict[str, list] = {}
    closed = 0
    for (asin,) in conn.execute("SELECT asin FROM products WHERE status != 'dead'").fetchall():
        v = engine.product_view(conn, asin)
        el = v.get("eligibility") or {}
        if v["gated"] == "blocked" and "not currently accepting" in (el.get("reason") or ""):
            closed += 1
        if v["gated"] != "approval":
            continue
        reason = el.get("reason") or ""
        for c in CAT_RE.findall(reason):
            cats.setdefault(c.strip(), []).append(v)
        if "approval to list in this brand" in reason:
            brands.setdefault(v["brand"] or "(brand unknown)", []).append(v)

    def summarize(groups: dict) -> list[dict]:
        out = []
        for name, items in groups.items():
            ranks = [v["snapshot"]["sales_rank"] for v in items if v["snapshot"] and v["snapshot"].get("sales_rank")]
            prices = [v["economics"]["sale_price"] for v in items if v["economics"].get("sale_price")]
            strong = [v for v in items if _strong(v)]
            url = next((v["eligibility"].get("approval_url") for v in items if (v.get("eligibility") or {}).get("approval_url")), "")
            out.append({"name": name, "products": len(items), "strong": len(strong),
                        "best_rank": min(ranks) if ranks else None,
                        "median_price": round(statistics.median(prices), 2) if prices else None,
                        "examples": [(v["title"] or v["asin"])[:60] for v in sorted(
                            strong or items, key=lambda v: (v["snapshot"] or {}).get("sales_rank") or 10**9)[:3]],
                        "approval_url": url})
        return sorted(out, key=lambda g: (-g["strong"], -g["products"]))

    return {"categories": summarize(cats), "brands": summarize(brands), "closed_to_applications": closed}
