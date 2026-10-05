"""Store price finder: Google Lens + Google Shopping, via SerpAPI.

Google Lens searches the Amazon product PHOTO and returns matching listings
at other stores with prices. That is what Stealth Seller's "AI searches"
panel shows (domain / title / price / thumbnail). Google Shopping searches by
UPC or title for your area as a second pass.

Same approach as Telly Travels (SerpAPI → Google data). One search returns
prices from many stores at once: Walmart, Target, Costco, Sam's Club, CVS…

Needs SERPAPI_KEY (serpapi.com; small free tier, then paid). Set your area
in the dashboard (Setup) or `gs location "Detroit, Michigan, United States"`.

Results are saved as retail sources marked "auto". Always check the pack
size matches the Amazon listing before you buy.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

from . import db
from .sources import search_query

API = "https://serpapi.com/search.json"

# Google Shopping store names → our retailer keys.
KNOWN = {
    "walmart": "walmart", "target": "target", "costco": "costco", "samsclub": "samsclub",
    "thehomedepot": "homedepot", "homedepot": "homedepot", "lowes": "lowes", "bestbuy": "bestbuy",
    "cvspharmacy": "cvs", "cvs": "cvs", "walgreens": "walgreens", "wholefoodsmarket": "wholefoods",
    "kohls": "kohls", "dollartree": "dollartree", "kroger": "kroger", "meijer": "meijer",
    "traderjoes": "traderjoes", "bjswholesaleclub": "bjs", "dollargeneral": "dollargeneral",
    "ulta": "ulta", "ultabeauty": "ulta", "staples": "staples", "petsmart": "petsmart", "petco": "petco",
}
SKIP = ("amazon", "ebay", "etsy", "aliexpress", "temu", "poshmark", "mercari")

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "and", "with", "for", "of", "oz", "ounce", "pack", "count", "ct", "in", "a", "by"}


def configured() -> bool:
    return bool(os.environ.get("SERPAPI_KEY"))


def store_key(name: str) -> str | None:
    n = (name or "").lower().split(" - ")[0].strip()
    n = re.sub(r"^(https?://)?(www\.)?", "", n)
    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", n):  # a domain like samsclub.com
        n = n.rsplit(".", 1)[0]
    n = re.sub(r"[^a-z]", "", n)
    if not n or any(s in n for s in SKIP):
        return None
    return KNOWN.get(n, n)


def _words(text: str) -> set[str]:
    t = (text or "").lower().replace("'", "").replace("’", "")   # peet's -> peets
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)                           # 32oz -> 32 oz
    return set(_WORD.findall(t))


def similarity(a: str, b: str) -> float:
    """Share of the Amazon title's meaningful words found in the store title."""
    wa = {w for w in _words(a) if w not in _STOP and len(w) > 1}
    wb = _words(b)
    return len(wa & wb) / len(wa) if wa else 0.0


def search(query: str, location: str | None = None) -> list[dict]:
    params = {"engine": "google_shopping", "q": query, "gl": "us", "hl": "en",
              "api_key": os.environ["SERPAPI_KEY"]}
    if location:
        params["location"] = location
    with urllib.request.urlopen(f"{API}?{urllib.parse.urlencode(params)}", timeout=60) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("shopping_results") or []


def search_lens(image_url: str) -> list[dict]:
    """Google Lens visual matches for a product photo, normalized to the shopping shape."""
    params = {"engine": "google_lens", "url": image_url, "hl": "en", "country": "us",
              "api_key": os.environ["SERPAPI_KEY"]}
    with urllib.request.urlopen(f"{API}?{urllib.parse.urlencode(params)}", timeout=60) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(data["error"])
    out = []
    for m in data.get("visual_matches") or []:
        price = (m.get("price") or {}).get("extracted_value")
        if price is None:
            continue
        out.append({"title": m.get("title", ""), "source": m.get("source", ""), "extracted_price": price,
                    "link": m.get("link", ""), "thumbnail": m.get("thumbnail", ""), "via": "lens"})
    return out


def pick_offers(results: list[dict], amazon_title: str, min_match: float = 0.5) -> list[dict]:
    """Cheapest close match per store."""
    best: dict[str, dict] = {}
    for r in results:
        key = store_key(r.get("source", ""))
        price = r.get("extracted_price")
        if not key or not isinstance(price, (int, float)):
            continue
        match = similarity(amazon_title, r.get("title", ""))
        # Lens already matched the photo, so its (often shorter) store titles need less word overlap.
        if match < (0.3 if r.get("via") == "lens" else min_match):
            continue
        if key not in best or price < best[key]["price"]:
            best[key] = {"retailer": key, "price": float(price), "title": r.get("title", ""),
                         "url": r.get("link") or r.get("product_link") or "", "match": round(match, 2),
                         "via": r.get("via", "shopping")}
    return sorted(best.values(), key=lambda o: o["price"])


def find_prices(conn, asins: list[str]) -> dict:
    location = db.get_setting(conn, "location")
    searched = saved = 0
    for asin in asins:
        p = conn.execute("SELECT title, upc, image_url FROM products WHERE asin = ?", (asin,)).fetchone()
        if not p or not p["title"]:
            continue
        results = search_lens(p["image_url"]) if p["image_url"] else []
        results += search(search_query(p["title"], p["upc"]), location)
        offers = pick_offers(results, p["title"])
        if not offers and p["upc"]:  # some stores don't index UPCs; retry by title
            offers = pick_offers(search(search_query(p["title"]), location), p["title"])
        searched += 1
        for o in offers[:6]:
            db.add_source(conn, asin, o["retailer"], o["price"], url=o["url"],
                          note=f"{o['via']} · {int(o['match'] * 100)}% match: {o['title'][:120]} — check pack size")
            saved += 1
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"prices_checked:{asin}", db.now()))
        conn.commit()
    return {"searched": searched, "prices_saved": saved}


def due_for_check(conn, limit: int = 50, days: float = 7) -> list[str]:
    """Live products whose prices haven't been auto-checked in `days`, best candidates first."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT p.asin FROM products p
           LEFT JOIN settings s ON s.key = 'prices_checked:' || p.asin
           LEFT JOIN (SELECT asin, MAX(id) mid FROM signals GROUP BY asin) l ON l.asin = p.asin
           LEFT JOIN signals g ON g.id = l.mid
           WHERE p.status NOT IN ('dead','pass') AND p.title IS NOT NULL
             AND (s.value IS NULL OR s.value < ?)
           ORDER BY COALESCE(g.score, 0) DESC LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [r[0] for r in rows]
