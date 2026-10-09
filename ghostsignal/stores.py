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
    "costcowholesale": "costco", "barnesnoble": "barnesandnoble", "tjmaxx": "tjmaxx", "tj": "tjmaxx",
    "olliesbargainoutlet": "ollies", "dickssportinggoods": "dickssportinggoods", "officedepotofficemax": "officedepot",
    "officedepot": "officedepot", "riteaid": "riteaid", "acehardware": "acehardware", "jcpenney": "jcpenney",
    "homegoods": "homegoods", "marshalls": "marshalls", "rossdressforless": "rossstores", "biglots": "biglots",
    "hobbylobby": "hobbylobby", "michaels": "michaels", "michaelsstores": "michaels", "fivebelow": "fivebelow",
    "familydollar": "familydollar", "tractorsupplyco": "tractorsupply", "tractorsupply": "tractorsupply",
    "gamestop": "gamestop", "newegg": "newegg", "neweggcom": "newegg", "heb": "heb", "hyvee": "hyvee", "aldi": "aldi",
}
SKIP = ("amazon", "ebay", "etsy", "aliexpress", "temu", "poshmark", "mercari")

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "and", "with", "for", "of", "oz", "ounce", "pack", "count", "ct", "in", "a", "by"}


def configured() -> bool:
    return bool(os.environ.get("SERPAPI_KEY"))


def store_key(name: str) -> str | None:
    if re.search(r"\s-\s*(seller|marketplace)", (name or "").lower()):   # third-party sellers on a store's marketplace
        return None
    n = (name or "").lower().split(" - ")[0].strip()
    n = re.sub(r"^(https?://)?(www\.)?", "", n)
    if re.fullmatch(r"[a-z0-9.-]+\.(com|net|org|us|co)", n):  # a domain like samsclub.com
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
    with urllib.request.urlopen(f"{API}?{urllib.parse.urlencode(params)}", timeout=90) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("shopping_results") or []


def search_lens(image_url: str) -> list[dict]:
    """Google Lens visual matches for a product photo, normalized to the shopping shape."""
    params = {"engine": "google_lens", "url": image_url, "hl": "en", "country": "us",
              "api_key": os.environ["SERPAPI_KEY"]}
    with urllib.request.urlopen(f"{API}?{urllib.parse.urlencode(params)}", timeout=90) as r:
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


_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(fl\.?\s*oz|oz|ounces?|lbs?|pounds?|kg|g|grams?|ml|l|liters?|ct|count|pk|pack|packs|pcs|pieces|rolls?|bags?|bars?)\b", re.I)
_TO_OZ = {"oz": 1, "ounce": 1, "ounces": 1, "lb": 16, "lbs": 16, "pound": 16, "pounds": 16, "kg": 35.274, "g": 0.035274,
          "gram": 0.035274, "grams": 0.035274}
_COUNT = {"ct", "count", "pk", "pack", "packs", "pcs", "pieces", "roll", "rolls", "bag", "bags", "bar", "bars"}


def sizes(title: str) -> dict:
    """{'oz': total weight in oz, 'ml': volume, 'count': biggest count} pulled from a product title."""
    out: dict = {}
    for num in re.findall(r"\b(?:pack|set|case|box|bundle)\s+of\s+(\d+)\b", (title or "").lower()):
        out["count"] = max(out.get("count", 0), float(num))          # "Pack of 2": the number comes after the word
    for num, unit in _SIZE.findall((title or "").lower().replace("fl. oz", "floz")):
        n, u = float(num), re.sub(r"[\s.]", "", unit)
        if u.startswith("floz") or u in ("ml", "l", "liter", "liters"):
            out["ml"] = n * (29.5735 if u.startswith("floz") else 1000 if u.startswith("l") else 1)
        elif u in _TO_OZ:
            out["oz"] = max(out.get("oz", 0), n * _TO_OZ[u])
        elif u in _COUNT:
            out["count"] = max(out.get("count", 0), n)
    return out


def total_size(s: dict) -> dict:
    """Compare what you actually get: 2.75 lb x pack of 2 = 88 oz total; a single 22 oz jar is not the same thing."""
    t = {}
    if "oz" in s:
        t["oz"] = s["oz"] * s.get("count", 1)
    if "ml" in s:
        t["ml"] = s["ml"] * s.get("count", 1)
    if "count" in s and "oz" not in s and "ml" not in s:
        t["count"] = s["count"]
    return t


def same_size(a: str, b: str, b_url: str = "") -> bool | None:
    """True/False when both listings state a comparable size; None when we can't tell. The store's size is often
    only in its URL slug (…/Raw-Honey-22-oz-…), so that counts too."""
    sb_src = b + " " + re.sub(r"[-_/]+", " ", (b_url or "").split("?")[0].rsplit("/", 2)[-2] if (b_url or "").count("/") > 3 else "")
    sa, sb = total_size(sizes(a)), total_size(sizes(sb_src))
    verdict = None
    for k in ("oz", "ml", "count"):
        if k in sa and k in sb:
            if abs(sa[k] - sb[k]) / max(sa[k], sb[k]) > 0.04:
                return False
            verdict = True
    if "oz" in sa and "oz" not in sb and "count" in sb and "count" not in sizes(a):
        return False         # store lists a count (e.g. 12 ct) for a product Amazon sells by weight: different thing
    return verdict


# Reputable retailers only: arbitrage buys need a real store receipt/invoice (also what ungating asks for), and
# marketplaces, used-book sites and tiny shops give fake "cheap" prices (used copies, samples, wrong packs).
RETAILERS_OK = {
    "walmart", "target", "costco", "samsclub", "bjs", "kroger", "meijer", "heb", "publix", "safeway", "albertsons",
    "hyvee", "wegmans", "foodlion", "giantfood", "stopandshop", "shoprite", "wholefoods", "traderjoes", "aldi",
    "cvs", "walgreens", "riteaid", "homedepot", "lowes", "menards", "acehardware", "tractorsupply", "bestbuy",
    "staples", "officedepot", "booksamillion", "barnesandnoble", "gamestop", "ulta", "sephora", "petsmart", "petco",
    "chewy", "michaels", "joann", "hobbylobby", "kohls", "macys", "jcpenney", "tjmaxx", "marshalls", "homegoods",
    "rossstores", "burlington", "ollies", "biglots", "dollartree", "dollargeneral", "familydollar", "fivebelow",
    "dickssportinggoods", "academy", "scheels", "golfgalaxy", "newegg", "bedbathandbeyond", "worldmarket", "crateandbarrel",
}


def product_overlap(a: str, b: str) -> float:
    """Share of the Amazon title's product words found in the store title."""
    wa = {w for w in _words(a) if w not in _STOP and len(w) > 2}
    wb = _words(b)
    return len(wa & wb) / len(wa) if wa else 0.0


def same_product(a: str, b: str, threshold: float = 0.6) -> bool:
    """Same brand and most of the product's words. Catches a different item that happens to share a size."""
    return product_overlap(a, b) >= threshold


MARKETPLACES = ("ebay", "mercari", "poshmark", "depop", "etsy", "aliexpress", "temu", "tiktokshop", "facebook", "offerup")


def classify_results(results: list[dict]) -> str:
    """What kind of sellers the search found: 'retail' (a real store had it), 'resellers' (only eBay/Mercari/3P
    marketplace sellers: an Amazon-first product), or 'nothing'."""
    if not results:
        return "nothing"
    retail = reseller = 0
    for r in results:
        src = (r.get("source") or "").lower()
        key = store_key(src)
        if key and key in RETAILERS_OK:
            retail += 1
        elif any(m in src.replace(" ", "") for m in MARKETPLACES) or " - " in src:
            reseller += 1
    if retail:
        return "retail"
    return "resellers" if reseller >= 3 else "nothing"


def _brand_words(brand: str | None) -> set[str]:
    return {w for w in _words(brand or "") if len(w) > 2 and w not in _STOP}


def pick_offers(results: list[dict], amazon_title: str, min_match: float = 0.5, brand: str | None = None) -> list[dict]:
    """Cheapest close match per store. A short generic title ("Classroom Calendar Days of the Year") matches any
    store item with those words, so it can't be matched by title at all; and when the brand is known, the store
    listing has to name it (a $10 no-name poster is not the brand's boxed card set)."""
    if len({w for w in _words(amazon_title) if w not in _STOP and len(w) > 2}) < 5:
        return []
    bw = _brand_words(brand) - {"generic"}
    best: dict[str, dict] = {}
    for r in results:
        key = store_key(r.get("source", ""))
        price = r.get("extracted_price")
        if not key or key not in RETAILERS_OK or not isinstance(price, (int, float)):
            continue
        if bw and not (bw & _words(r.get("title", ""))):
            continue
        match = similarity(amazon_title, r.get("title", ""))
        # Lens already matched the photo, so its (often shorter) store titles need less word overlap.
        if match < (0.3 if r.get("via") == "lens" else min_match):
            continue
        size_ok = same_size(amazon_title, r.get("title", ""), r.get("link") or r.get("product_link") or "")
        if size_ok is False:          # a different pack size is never a match
            continue
        if size_ok is True:           # size confirmed: needs most of the product words to match
            if not same_product(amazon_title, r.get("title", "")):
                continue
        else:                         # size not stated: only a close product match, shown as "likely" and unverified
            if product_overlap(amazon_title, r.get("title", "")) < 0.75:
                continue
        if key not in best or price < best[key]["price"]:
            best[key] = {"retailer": key, "price": float(price), "title": r.get("title", ""),
                         "url": r.get("product_link") or r.get("link") or "", "match": round(match, 2),
                         "store_title": r.get("title", ""),
                         "via": r.get("via", "shopping"), "size_ok": size_ok}
    return sorted(best.values(), key=lambda o: o["price"])


def searches_left() -> int | None:
    """SerpAPI searches left this month (the account endpoint itself is free)."""
    try:
        with urllib.request.urlopen(f"https://serpapi.com/account.json?api_key={os.environ['SERPAPI_KEY']}", timeout=30) as r:
            return int(json.loads(r.read()).get("total_searches_left"))
    except Exception:
        return None


def _safe(fn, *args) -> list[dict]:
    """One slow or failed search shouldn't sink the whole price check."""
    try:
        return fn(*args)
    except Exception as e:
        print(f"  store search skipped: {str(e)[:120]}", flush=True)
        return []


def find_prices(conn, asins: list[str], budget: int | None = None) -> dict:
    """Cheapest matching store offers per product. Spends as few searches as possible: barcode/title shopping
    search first, title-only retry, and the image (Lens) search only when nothing matched. Stops at `budget`."""
    location = db.get_setting(conn, "location")
    left = searches_left()
    reserve = int(os.environ.get("SERPAPI_RESERVE", "10"))      # keep a few for manual "Find store prices"
    if budget is None:
        spare = max(0, left - reserve) if left is not None else 10
        if len(asins) > 1:   # automatic runs: spread what's left over the rest of the month
            import calendar
            from datetime import date
            today = date.today()
            days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
            spare = -(-spare // days_left)   # ceil
        budget = spare
    searched = saved = used = 0
    for asin in asins:
        if used >= budget:
            break
        p = conn.execute("SELECT title, brand, upc, image_url FROM products WHERE asin = ?", (asin,)).fetchone()
        if not p or not p["title"]:
            continue
        # Title search is fast (~1s); barcode search can take 30s+, so it's the fallback, then the photo search.
        raw = _safe(search, search_query(p["title"]), location); used += 1
        offers = pick_offers(raw, p["title"], brand=p["brand"])
        if not offers and p["upc"] and used < budget:
            more = _safe(search, search_query(p["title"], p["upc"]), location); used += 1
            raw += more; offers = pick_offers(more, p["title"], brand=p["brand"])
        if not offers and p["image_url"] and used < budget:
            more = _safe(search_lens, p["image_url"]); used += 1
            raw += more; offers = pick_offers(more, p["title"], brand=p["brand"])
        searched += 1
        kind = classify_results(raw)
        note = ("matched" if offers else
                "no_match_retail" if kind == "retail" else      # stores had similar items, but not this exact one
                "resellers_only" if kind == "resellers" else "nothing_found")
        conn.execute("UPDATE products SET store_note = ? WHERE asin = ?", (f"{note}@{db.now()}", asin))
        for o in offers[:10]:
            db.add_source(conn, asin, o["retailer"], o["price"], url=o["url"],
                          note=f"{o['via']} · {int(o['match'] * 100)}% match"
                               + (" · same size" if o.get("size_ok") else " · size not stated, check it")
                               + f": {o['title'][:120]}")
            saved += 1
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"prices_checked:{asin}", db.now()))
        conn.commit()
    return {"searched": searched, "prices_saved": saved, "searches_used": used, "searches_left_before": left}


def due_for_check(conn, limit: int = 8, days: float = 14) -> list[str]:
    """Only real candidates get a (scarce) store-price search: sellable or one approval away, not junk, not an
    Amazon-only brand, Amazon not on the listing, selling (rank <= 80k) and $18+. Best first, not checked lately."""
    from datetime import datetime, timedelta, timezone
    from . import engine
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    checked = {r[0][len("prices_checked:"):]: r[1] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'prices_checked:%'")}
    picks = []
    for (asin,) in conn.execute("SELECT asin FROM products WHERE status NOT IN ('dead','pass') AND title IS NOT NULL"):
        if checked.get(asin, "") >= cutoff:
            continue
        v = engine.list_view(conn, asin)
        s, e = v["snapshot"] or {}, v["economics"]
        if v["restricted"] or v["gated"] not in ("ungated", "approval") or (v.get("enrichment") or {}).get("sold_in_stores") is False:
            continue
        if s.get("amazon_price") or not s.get("sales_rank") or s["sales_rank"] > 80_000 or (e.get("sale_price") or 0) < 18:
            continue
        picks.append(((1 if v["gated"] == "ungated" else 0), -s["sales_rank"], asin))
    return [a for *_, a in sorted(picks, reverse=True)[:limit]]


# ---------------------------------------------------------------- Amazon storefronts (no Keepa needed)

def parse_storefront(data: dict) -> list[dict]:
    out = []
    for r in data.get("organic_results") or []:
        if not r.get("asin"):
            continue
        out.append({"asin": r["asin"], "title": r.get("title", ""), "price": r.get("extracted_price"),
                    "image_url": r.get("thumbnail", ""), "rating": r.get("rating"), "reviews": r.get("reviews")})
    return out


def amazon_storefront(seller_id: str, pages: int = 3) -> tuple[list[dict], int]:
    """Every product a seller lists, 16 per page, 1 SerpAPI search per page. -> (products, searches used).
    Uses Amazon's own seller filter (rh=p_6:<seller>) through SerpAPI's Amazon engine."""
    items, used = [], 0
    for page in range(1, pages + 1):
        params = {"engine": "amazon", "amazon_domain": "amazon.com", "k": "*", "rh": f"p_6:{seller_id}",
                  "page": page, "api_key": os.environ["SERPAPI_KEY"]}
        with urllib.request.urlopen(f"{API}?{urllib.parse.urlencode(params)}", timeout=90) as r:
            data = json.loads(r.read())
        used += 1
        if data.get("error"):
            raise RuntimeError(data["error"])
        got = parse_storefront(data)
        items += got
        if not got or not (data.get("serpapi_pagination") or {}).get("next"):
            break
    seen = set()
    return [i for i in items if not (i["asin"] in seen or seen.add(i["asin"]))], used
