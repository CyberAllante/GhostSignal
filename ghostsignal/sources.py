"""Retailer search links — one click from a product to every store that might carry it.

We generate search URLs (UPC when known, otherwise a cleaned title) rather than
scraping retailers. You verify price/stock, then log it with `gs source add`.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

RETAILERS = {
    "walmart":    ("Walmart",     "https://www.walmart.com/search?q={q}"),
    "target":     ("Target",      "https://www.target.com/s?searchTerm={q}"),
    "costco":     ("Costco",      "https://www.costco.com/CatalogSearch?keyword={q}"),
    "samsclub":   ("Sam's Club",  "https://www.samsclub.com/s/{q}"),
    "homedepot":  ("Home Depot",  "https://www.homedepot.com/s/{q}"),
    "lowes":      ("Lowe's",      "https://www.lowes.com/search?searchTerm={q}"),
    "bestbuy":    ("Best Buy",    "https://www.bestbuy.com/site/searchpage.jsp?st={q}"),
    "cvs":        ("CVS",         "https://www.cvs.com/search?searchTerm={q}"),
    "walgreens":  ("Walgreens",   "https://www.walgreens.com/search/results.jsp?Ntt={q}"),
    "traderjoes": ("Trader Joe's", "https://www.traderjoes.com/home/search?q={q}&section=products"),
    "wholefoods": ("Whole Foods", "https://www.wholefoodsmarket.com/search?text={q}"),
    "kohls":      ("Kohl's",      "https://www.kohls.com/search.jsp?search={q}"),
    "dollartree": ("Dollar Tree", "https://www.dollartree.com/searchresults?Ntt={q}"),
    "kroger":     ("Kroger",      "https://www.kroger.com/search?query={q}"),
    "meijer":     ("Meijer",      "https://www.meijer.com/shopping/search.html?text={q}"),
    "bjs":        ("BJ's",        "https://www.bjs.com/search/{q}"),
    "dollargeneral": ("Dollar General", "https://www.dollargeneral.com/search?q={q}"),
    "fivebelow":  ("Five Below",  "https://www.fivebelow.com/search?q={q}"),
    "ollies":     ("Ollie's",     "https://www.ollies.us/search?q={q}"),
    "tjmaxx":     ("TJ Maxx",     "https://tjmaxx.tjx.com/store/shop?q={q}"),
    "ulta":       ("Ulta",        "https://www.ulta.com/search?search={q}"),
    "petsmart":   ("PetSmart",    "https://www.petsmart.com/search/?q={q}"),
    "michaels":   ("Michaels",    "https://www.michaels.com/search?q={q}"),
    "google":     ("Google Shopping", "https://www.google.com/search?tbm=shop&q={q}"),
}

_NOISE = re.compile(r"\b(pack of \d+|\d+\s*(?:count|ct|pack|pk|oz|ounce|ounces|lb|fl oz)s?)\b|[,()\[\]|]", re.I)


def search_query(title: str | None, upc: str | None = None, max_words: int = 8) -> str:
    if upc:
        return upc
    t = _NOISE.sub(" ", title or "")
    return " ".join(t.split()[:max_words])


def retailer_links(title: str | None, upc: str | None = None) -> dict[str, dict]:
    q = quote_plus(search_query(title, upc))
    tq = quote_plus(search_query(title))  # some stores don't index UPCs; give a title link too
    out = {}
    for key, (name, tmpl) in RETAILERS.items():
        out[key] = {"name": name, "url": tmpl.format(q=q), "title_url": tmpl.format(q=tq)}
    return out


def marketplace_links(asin: str, title: str | None = None) -> dict[str, str]:
    q = quote_plus(search_query(title))
    common = {
        "ebay_sold": f"https://www.ebay.com/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1",
        "ebay_active": f"https://www.ebay.com/sch/i.html?_nkw={q}",
        "facebook": f"https://www.facebook.com/marketplace/search/?query={q}",
        "amazon_search": f"https://www.amazon.com/s?k={q}",
    }
    if asin.startswith("GS"):  # not an Amazon product
        return common
    return {**common,
        "amazon": f"https://www.amazon.com/dp/{asin}",
        "offers": f"https://www.amazon.com/gp/offer-listing/{asin}",
        "keepa": f"https://keepa.com/#!product/1-{asin}",
        "sellercentral": f"https://sellercentral.amazon.com/product-search/search?q={asin}",
    }
