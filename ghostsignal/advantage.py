"""Your edge over a brand-new seller account.

Amazon decides gating per account, and it doesn't publish what a brand-new account can sell. So we compare
your real answers (from the Seller API restrictions check) against a baseline of categories and brands that
sellers widely report as gated for new accounts. Anything you can sell that's on that baseline is an edge:
a new seller would need distributor invoices and an application to sell it.

The baseline is a starting point, not Amazon's rules. Edit it with the "new_seller_baseline" setting
(one brand or category per line) once you learn more.
"""

from __future__ import annotations

import re

from . import db, engine, restrictions, ungate

# (label, regex on the product's category, why it matters)
CATEGORIES = [
    ("Grocery & Gourmet Food", r"grocery|gourmet", "Commonly needs approval for new accounts"),
    ("Toys & Games", r"\btoys?\b", "Extra seller rules during the Nov-Jan holiday season"),
    ("Beauty & Personal Care", r"beauty|personal care", "Topicals and some brands need approval"),
    ("Health & Household", r"health|household", "Topicals, supplements and some brands need approval"),
    ("Automotive & Powersports", r"automotive|powersports", "Needs approval"),
    ("Jewelry", r"jewelry", "Needs approval"),
    ("Watches", r"\bwatch(es)?\b", "Needs approval"),
    ("Collectibles & Fine Art", r"collectible|fine art", "Needs approval"),
    ("Music & Video", r"\bmusic\b|\bdvd|blu-?ray|movies", "Needs approval"),
]

# Brands sellers widely report as gated for new accounts. Not an Amazon list.
BRANDS = [
    "Mattel", "Fisher-Price", "Hot Wheels", "Barbie", "Hasbro", "Nerf", "Play-Doh", "LEGO", "Disney", "Crayola",
    "Funko", "Melissa & Doug", "Nike", "Adidas", "Under Armour", "The North Face", "YETI", "Stanley", "KitchenAid",
    "Pampers", "Huggies", "Gillette", "Oral-B", "Olay", "Neutrogena", "CeraVe", "Burt's Bees", "Apple", "Bose",
]
SETTING = "new_seller_baseline"


def _brand_baseline(conn) -> list[str]:
    extra = restrictions.parse_list(db.get_setting(conn, SETTING))
    return BRANDS + extra


def _match_brand(brand: str, baseline: list[str]) -> str:
    nb = restrictions._norm(brand)
    if not nb:
        return ""
    for b in baseline:
        nbl = restrictions._norm(b)
        if nbl and (nb == nbl or (len(nbl) >= 4 and nb.startswith(nbl))):
            return b
    return ""


def _match_category(category: str, extra: list[str]) -> tuple[str, str]:
    for label, pat, why in CATEGORIES:
        if re.search(pat, category or "", re.I):
            return label, why
    for c in extra:   # user-added baseline lines can name categories too
        if c and c.lower() in (category or "").lower():
            return c, "On your baseline list"
    return "", ""


def _brief(v: dict) -> dict:
    return {"asin": v["asin"], "title": (v["title"] or v["asin"])[:80], "brand": v["brand"], "category": v["category"],
            "sales_rank": (v["snapshot"] or {}).get("sales_rank"), "sale_price": v["economics"].get("sale_price")}


def report(conn) -> dict:
    baseline = _brand_baseline(conn)
    extra = restrictions.parse_list(db.get_setting(conn, SETTING))
    counts = {"ungated": 0, "approval": 0, "blocked": 0}
    from_amazon = 0
    cats: dict[str, dict] = {}
    brands: dict[str, dict] = {}
    open_brands: dict[str, int] = {}
    edge: dict[str, dict] = {}
    rows = conn.execute("""SELECT e.asin, e.source FROM eligibility e JOIN products p ON p.asin = e.asin
                           WHERE p.status != 'dead'""").fetchall()
    for asin, source in rows:
        v = engine.list_view(conn, asin)
        if v["restricted"]:
            continue
        status = (v.get("eligibility") or {}).get("status")
        if status not in counts:
            continue
        counts[status] += 1
        from_amazon += source == "spapi"
        if status != "ungated":
            continue
        if v["brand"]:
            open_brands[v["brand"]] = open_brands.get(v["brand"], 0) + 1
        strong = ungate._strong(v)
        cat, why = _match_category(v["category"], extra)
        if cat:
            g = cats.setdefault(cat, {"name": cat, "why": why, "can_sell": 0, "strong": 0, "items": []})
            g["can_sell"] += 1
            g["strong"] += strong
            g["items"].append(v)
            edge[asin] = v
        b = _match_brand(v["brand"], baseline)
        if b:
            g = brands.setdefault(b, {"name": b, "why": "Commonly gated for new accounts", "can_sell": 0, "strong": 0, "items": []})
            g["can_sell"] += 1
            g["strong"] += strong
            g["items"].append(v)
            edge[asin] = v

    def rank(v):
        return (v["snapshot"] or {}).get("sales_rank") or 10**9

    def finish(groups: dict) -> list[dict]:
        out = []
        for g in groups.values():
            items = sorted(g.pop("items"), key=lambda v: (not ungate._strong(v), rank(v)))
            g["examples"] = [(v["title"] or v["asin"])[:60] for v in items[:3]]
            out.append(g)
        return sorted(out, key=lambda g: (-g["strong"], -g["can_sell"]))

    top = sorted(edge.values(), key=lambda v: (not ungate._strong(v), rank(v)))
    checked = sum(counts.values())
    return {
        "checked": checked, "from_amazon": from_amazon,
        "can_sell": counts["ungated"], "need_approval": counts["approval"], "cant_sell": counts["blocked"],
        "edge_products": len(edge), "edge_strong": sum(ungate._strong(v) for v in edge.values()),
        "categories": finish(cats), "brands": finish(brands),
        "top": [_brief(v) for v in top[:12]],
        "open_brands": [{"name": n, "products": c} for n, c in sorted(open_brands.items(), key=lambda x: -x[1])[:20]],
        "note": ("A brand-new account is compared using categories and brands sellers widely report as gated for "
                 "new accounts. Amazon doesn't publish that list, so treat it as a guide. Your own answers come "
                 "from Amazon's Seller API for your account."),
    }


def summary_text(r: dict, limit: int = 8) -> str:
    if not r["checked"]:
        return ("No gating answers yet. Run a data refresh with the Amazon Seller API keys set, so every product "
                "gets checked against your account.")
    lines = [f"Checked {r['checked']} products ({r['from_amazon']} straight from Amazon for your account): "
             f"{r['can_sell']} you can sell, {r['need_approval']} need approval, {r['cant_sell']} you can't sell.",
             f"EDGE: {r['edge_products']} products you can sell are in categories or brands new accounts usually "
             f"have to apply for ({r['edge_strong']} of them strong: rank under 50,000 and $10+)."]
    if r["categories"]:
        lines.append("Categories open to you that new accounts usually can't sell:")
        lines += [f"- {g['name']}: {g['can_sell']} products ({g['strong']} strong). {g['why']}."
                  + (f" e.g. {'; '.join(g['examples'][:2])}" if g["examples"] else "") for g in r["categories"][:limit]]
    if r["brands"]:
        lines.append("Brands open to you that new accounts usually can't sell:")
        lines += [f"- {g['name']}: {g['can_sell']} products ({g['strong']} strong)"
                  + (f". e.g. {'; '.join(g['examples'][:2])}" if g["examples"] else "") for g in r["brands"][:limit]]
    if r["open_brands"]:
        lines.append("All brands you can sell, most products first: "
                     + ", ".join(f"{b['name']} ({b['products']})" for b in r["open_brands"][:15]))
    lines.append(r["note"])
    return "\n".join(lines)
