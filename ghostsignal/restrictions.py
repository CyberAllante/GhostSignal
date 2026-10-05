"""Products you can never (or shouldn't) resell: Amazon's own brands plus your own blocklist.

Amazon private-label items (Amazon Basics, Solimo, Happy Belly...) are sold only by Amazon, so
there's nothing to arbitrage. They're forced to PASS and can be bulk-archived from Products > Clean up.
"""

from __future__ import annotations

import re

# Brand field match (normalized: lowercase, letters/digits only).
AMAZON_BRANDS = {
    "amazonbasics", "amazonbasic", "amazon", "amazonessentials", "amazonelements", "amazoncollection",
    "amazonaspects", "amazonfresh", "amazondevices", "amazonbrand", "findbyamazon", "byamazon", "solimo",
    "happybelly", "mamabear", "wag", "pinzon", "goodthreads", "core10", "dailyritual", "larkro", "lark&ro",
    "arabella", "belei", "buttoneddown", "spottedzebra", "rivet", "stoneandbeam", "wickedlyprime",
    "365bywholefoodsmarket", "365everydayvalue", "kindle", "eero",
}
# Title phrases that mean the same thing.
TITLE_PHRASES = re.compile(
    r"\b(amazon\s?basics?|amazon\s?essentials|amazon\s?elements|by\s+amazon|amazon\s+brand|"
    r"solimo|happy\s+belly|mama\s+bear|wickedly\s+prime|amazon\s+fresh)\b", re.I)

SETTING = "blocked_brands"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9&]", "", (s or "").lower())


def parse_list(text: str | None) -> list[str]:
    return [l.strip() for l in re.split(r"[\n,]", text or "") if l.strip()]


def check(product: dict, custom: list[str] | None = None) -> str:
    """Return a human reason if this product can't/shouldn't be resold, else ''."""
    brand, title = product.get("brand") or "", product.get("title") or ""
    nb = _norm(brand)
    if nb in AMAZON_BRANDS:
        return f"Amazon-owned brand ({brand}) — only Amazon can sell it"
    if TITLE_PHRASES.search(title) and (not nb or nb in AMAZON_BRANDS):
        return "Amazon private label — only Amazon can sell it"
    for b in custom or []:
        nbl = _norm(b)
        if nbl and (nb == nbl or (not nb and _norm(title).startswith(nbl)) or (nbl in nb and len(nbl) >= 5)):
            return f"On your blocked list ({b})"
    return ""
