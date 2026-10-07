"""Ungating opportunities: which approvals would unlock the most good products.

Amazon's restriction reasons name what you need ("approval to list in the Grocery & Gourmet Foods category",
"approval to list in this brand"). One category approval unlocks every product in it; a brand approval unlocks
that brand. We group the gated products by those requirements and rank them by how many *strong* products
(real demand, decent price) each approval would open up.
"""

from __future__ import annotations

import json
import re
import statistics

from . import db, engine

STATUSES = ("not_started", "applying", "approved", "rejected")


def approvals(conn) -> dict:
    return json.loads(db.get_setting(conn, "approvals") or "{}")


def set_approval(conn, name: str, status: str, note: str | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    data = approvals(conn)
    cur = data.get(name, {})
    cur.update(status=status, updated=db.now())
    if note is not None:
        cur["note"] = note
    data[name] = cur
    db.set_setting(conn, "approvals", json.dumps(data))
    if status == "approved":   # re-check everything this approval could unlock
        conn.execute("""DELETE FROM eligibility WHERE source = 'spapi' AND status != 'ungated' AND (
                          lower(reason) LIKE ? OR asin IN (SELECT asin FROM products WHERE lower(brand) = lower(?)))""",
                     (f"%{name.lower()}%", name))
    return cur


CAT_RE = re.compile(r"approval to list in the (.+?) category", re.I)
STRONG_RANK, STRONG_PRICE = 50_000, 10.0


def _strong(v: dict) -> bool:
    s, e = v["snapshot"] or {}, v["economics"]
    return bool(s.get("sales_rank") and s["sales_rank"] <= STRONG_RANK and (e.get("sale_price") or 0) >= STRONG_PRICE)


def targets(conn) -> dict:
    cats: dict[str, list] = {}
    brands: dict[str, list] = {}
    limited: dict[str, list] = {}
    closed = 0
    for (asin,) in conn.execute("SELECT asin FROM products WHERE status != 'dead'").fetchall():
        v = engine.list_view(conn, asin)
        el = v.get("eligibility") or {}
        if v["gated"] == "blocked" and "not accepting applications" in (el.get("reason") or ""):
            closed += 1
        reason = el.get("reason") or ""
        if v["gated"] == "limited":
            limited.setdefault(v["brand"] or "(brand unknown)", []).append(v)
            continue
        if v["gated"] != "approval":
            continue
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
                        "approval_url": url,
                        "items": [{"asin": v["asin"], "title": v["title"], "image_url": v["image_url"],
                                   "sale_price": v["economics"].get("sale_price"), "max_cost": v["economics"].get("max_cost"),
                                   "rank": (v["snapshot"] or {}).get("sales_rank"), "offers": (v["snapshot"] or {}).get("offer_count"),
                                   "strong": _strong(v)}
                                  for v in sorted(items, key=lambda v: -(v.get("priority") or 0))]})
        return sorted(out, key=lambda g: (-g["strong"], -g["products"]))

    track = approvals(conn)
    out = {"categories": summarize(cats), "brands": summarize(brands), "limited": summarize(limited),
           "closed_to_applications": closed}
    for g in out["categories"] + out["brands"] + out["limited"]:
        g["tracking"] = track.get(g["name"], {"status": "not_started"})
    return out
