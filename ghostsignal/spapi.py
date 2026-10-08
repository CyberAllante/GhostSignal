"""Amazon Selling Partner API (SP-API): the official, free API for your seller account.

What we use it for:
  - Market data (free Keepa stand-in): Catalog Items gives title/brand/image/category/sales rank,
    Pricing item-offers gives Buy Box price, offer counts and whether Amazon is selling it.
    No price history or sales/month — those still need Keepa.
  - Gated check: Listings Restrictions API tells us, for YOUR account, whether
    an ASIN is ungated, needs approval, or can't be sold.
  - Real fees: Product Fees API returns Amazon's actual referral + FBA fee.

Setup (one time, free — needs a Professional seller account):
  Seller Central → Apps and Services → Develop Apps → register as a private
  developer → create an app → "Authorize" it for your own account.
  Put these in .env:
    SPAPI_CLIENT_ID, SPAPI_CLIENT_SECRET, SPAPI_REFRESH_TOKEN, SPAPI_SELLER_ID
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
MARKETPLACE_US = "ATVPDKIKX0DER"
ENV = ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN", "SPAPI_SELLER_ID")

_token: dict = {}


class SPAPIError(RuntimeError):
    pass


def configured() -> bool:
    return all(os.environ.get(k) for k in ENV)


def _access_token() -> str:
    if _token.get("value") and _token["expires"] > time.time() + 60:
        return _token["value"]
    missing = [k for k in ENV if not os.environ.get(k)]
    if missing:
        raise SPAPIError(f"Missing {', '.join(missing)} — see `gs setup`")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": os.environ["SPAPI_REFRESH_TOKEN"],
        "client_id": os.environ["SPAPI_CLIENT_ID"],
        "client_secret": os.environ["SPAPI_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request("https://api.amazon.com/auth/o2/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    _token.update(value=data["access_token"], expires=time.time() + int(data.get("expires_in", 3600)))
    return _token["value"]


def _call(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = ENDPOINT + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(4):
        req = urllib.request.Request(
            url, method=method, data=json.dumps(body).encode() if body else None,
            headers={"x-amz-access-token": _access_token(), "Content-Type": "application/json",
                     "User-Agent": "GhostSignal/0.1 (Language=Python)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise SPAPIError(f"{e.code} {path}: {e.read()[:300]!r}") from e
    raise SPAPIError("rate limited")


def parse_restrictions(data: dict) -> tuple[str, str, str]:
    """-> (status, reason, approval_url). No restrictions means you're good to sell."""
    restrictions = data.get("restrictions") or []
    if not restrictions:
        return "ungated", "", ""
    reasons = [r for x in restrictions for r in (x.get("reasons") or [])]
    codes = {r.get("reasonCode") for r in reasons}
    msg = "; ".join(r.get("message", "") for r in reasons)[:300]
    url = next((link.get("resource") for r in reasons for link in (r.get("links") or [])
                if link.get("resource")), "")
    if "APPROVAL_REQUIRED" in codes:
        return "approval", msg, url
    return status_from_reason(msg), msg, url


def status_from_reason(msg: str) -> str:
    """NOT_ELIGIBLE splits two ways. 'Not currently accepting applications' means the brand is closed: blocked.
    Anything else ('other listing limitations' + needs approval) is usually new-account limits that ease
    with sales history: limited."""
    return "blocked" if "not accepting applications" in (msg or "").lower() else "limited"


def check_gated(conn, asins: list[str]) -> dict:
    counts = {"ungated": 0, "approval": 0, "limited": 0, "blocked": 0}
    for asin in asins:
        data = _call("GET", "/listings/2021-08-01/restrictions", {
            "asin": asin, "sellerId": os.environ["SPAPI_SELLER_ID"],
            "marketplaceIds": MARKETPLACE_US, "conditionType": "new_new",
        })
        status, reason, url = parse_restrictions(data)
        db.set_eligibility(conn, asin, status, "spapi", reason, url)
        counts[status] += 1
        conn.commit()
        time.sleep(0.25)  # API allows ~5 requests/second
    return counts


def parse_fees(data: dict) -> tuple[float | None, float | None]:
    """-> (referral fee $, FBA fee $) from a getMyFeesEstimateForASIN response."""
    est = ((data.get("payload") or {}).get("FeesEstimateResult") or {}).get("FeesEstimate") or {}
    ref = fba = None
    for d in est.get("FeeDetailList") or []:
        amt = (d.get("FinalFee") or d.get("FeeAmount") or {}).get("Amount")
        if d.get("FeeType") == "ReferralFee":
            ref = amt
        elif d.get("FeeType") == "FBAFees":
            fba = amt
    return ref, fba


def update_fees(conn, asins: list[str]) -> int:
    """Store Amazon's real fees on a new snapshot (keeps the latest price/rank data)."""
    done = 0
    for asin in asins:
        snap = db.latest_snapshot(conn, asin)
        price = snap and (snap["buy_box"] or snap["avg_price_90"])
        if not price:
            continue
        data = _call("POST", f"/products/fees/v0/items/{asin}/feesEstimate", body={"FeesEstimateRequest": {
            "MarketplaceId": MARKETPLACE_US, "IsAmazonFulfilled": True, "Identifier": f"gs-{asin}",
            "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": "USD", "Amount": price}},
        }})
        ref, fba = parse_fees(data)
        if ref is None and fba is None:
            continue
        keep = {k: snap[k] for k in ("buy_box", "amazon_price", "avg_price_90", "sales_rank", "monthly_sold",
                                     "offer_count", "fba_offers", "fbm_offers", "ebay_sold_price")}
        db.add_snapshot(conn, asin, "spapi-fees", referral_pct=round(ref / price, 4) if ref else None,
                        fba_fee=fba, **keep)
        conn.commit()
        done += 1
        time.sleep(1.1)  # fees API allows ~1 request/second
    return done


# ---------------------------------------------------------------- market data (Keepa stand-in)

# Amazon's own retail seller IDs as they appear in offer data. Verified 10/7 against Echo Dot, Charmin, Tide,
# Pringles, Bounty listings (all A2R2RITDJNW1Q6); ATVPDKIKX0DER is the marketplace ID, kept for older data.
AMAZON_SELLER_IDS = db.AMAZON_SELLER_IDS
_CARRY = ("avg_price_90", "monthly_sold", "referral_pct", "fba_fee", "ebay_sold_price")


def _amt(x) -> float | None:
    v = (x or {}).get("Amount")
    return float(v) if v is not None else None


def _landed(price: dict | None) -> float | None:
    """Landed price if present (price + shipping), else listing price."""
    return _amt((price or {}).get("LandedPrice")) or _amt((price or {}).get("ListingPrice"))


def parse_catalog_item(item: dict) -> tuple[dict, int | None, dict]:
    """-> (product fields, overall sales rank, extra). The overall rank comes only from the top-level display
    group (e.g. #15,929 in Home & Kitchen). Sub-category ranks (#1 in Laptop Sleeves) are not comparable, so
    they go in `extra` and never feed the demand score."""
    pick = lambda arr: next((x for x in arr or [] if x.get("marketplaceId") == MARKETPLACE_US), (arr or [{}])[0] if arr else {})
    summ, ranks, imgs = pick(item.get("summaries")), pick(item.get("salesRanks")), pick(item.get("images"))
    group, classes = ranks.get("displayGroupRanks") or [], ranks.get("classificationRanks") or []
    rank = group[0].get("rank") if group else None
    sub = classes[0] if classes else {}
    main_cat = (group[0].get("title") if group else "") or (sub.get("title") or "") or (summ.get("browseClassification") or {}).get("displayName") or ""
    main = next((i for i in imgs.get("images") or [] if i.get("variant") == "MAIN"), None)
    ids = pick(item.get("identifiers")).get("identifiers") or []
    by_type = {i.get("identifierType"): i.get("identifier") for i in ids}
    upc = by_type.get("UPC") or by_type.get("EAN") or by_type.get("GTIN")
    dims = pick(item.get("dimensions"))
    w = (dims.get("package") or {}).get("weight") or (dims.get("item") or {}).get("weight") or {}
    to_lb = {"pounds": 1.0, "ounces": 1 / 16, "grams": 1 / 453.592, "kilograms": 2.20462}
    weight_lb = round(float(w["value"]) * to_lb[w["unit"]], 3) if w.get("value") and w.get("unit") in to_lb else None
    product = {"title": summ.get("itemName"), "brand": summ.get("brand") or summ.get("manufacturer"),
               "category": main_cat, "image_url": (main or {}).get("link"), "upc": upc, "weight_lb": weight_lb}
    return product, rank, {"sub_rank": sub.get("rank"), "sub_category": sub.get("title")}


def parse_offers(payload: dict) -> dict:
    """-> snapshot fields from a getItemOffers payload (Buy Box, offer counts, Amazon on listing)."""
    summ = payload.get("Summary") or {}
    offers = payload.get("Offers") or []
    box = next((b for b in summ.get("BuyBoxPrices") or [] if str(b.get("condition", "")).lower() == "new"), None)
    buy_box = _landed(box)
    if buy_box is None:   # fall back to the Buy Box winner, then the cheapest new offer
        win = next((o for o in offers if o.get("IsBuyBoxWinner")), None)
        if win:
            buy_box = (_amt(win.get("ListingPrice")) or 0) + (_amt(win.get("Shipping")) or 0) or None
    if buy_box is None:
        lows = [_landed(l) for l in summ.get("LowestPrices") or [] if str(l.get("condition", "")).lower() == "new"]
        lows = [l for l in lows if l]
        buy_box = min(lows) if lows else None
    by_channel = {"fba": 0, "fbm": 0}
    for n in summ.get("NumberOfOffers") or []:
        if str(n.get("condition", "")).lower() != "new":
            continue
        by_channel["fba" if str(n.get("fulfillmentChannel", "")).lower() == "amazon" else "fbm"] += int(n.get("OfferCount") or 0)
    total = by_channel["fba"] + by_channel["fbm"] or summ.get("TotalOfferCount")
    amazon = next((o for o in offers if o.get("SellerId") in AMAZON_SELLER_IDS), None)
    return {"buy_box": buy_box, "offer_count": total, "fba_offers": by_channel["fba"], "fbm_offers": by_channel["fbm"],
            "amazon_price": ((_amt(amazon.get("ListingPrice")) or 0) + (_amt(amazon.get("Shipping")) or 0)) or None
            if amazon else None}


def _catalog(asins: list[str]) -> dict:
    data = _call("GET", "/catalog/2022-04-01/items", {
        "identifiers": ",".join(asins), "identifiersType": "ASIN", "marketplaceIds": MARKETPLACE_US,
        "includedData": "summaries,salesRanks,images,identifiers,dimensions", "pageSize": 20})
    return {i["asin"]: i for i in data.get("items") or [] if i.get("asin")}


def _offers(asins: list[str]) -> dict:
    data = _call("POST", "/batches/products/pricing/v0/itemOffers", body={"requests": [{
        "uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET",
        "MarketplaceId": MARKETPLACE_US, "ItemCondition": "New", "CustomerType": "Consumer"} for a in asins]})
    out = {}
    for r in data.get("responses") or []:
        payload = (r.get("body") or {}).get("payload") or {}
        if (r.get("status") or {}).get("statusCode") == 200 and payload.get("ASIN"):
            out[payload["ASIN"]] = payload
    return out


def refresh_market(conn, asins: list[str]) -> dict:
    """Pull title/brand/image/rank + Buy Box/offers for ASINs and store a snapshot. Free, no Keepa needed."""
    done = skipped = 0
    asins = [a for a in dict.fromkeys(asins) if len(a) == 10 and not a.startswith("GS")]
    for i in range(0, len(asins), 20):
        batch = asins[i:i + 20]
        items = _catalog(batch)
        time.sleep(0.6)
        offers = _offers(batch)
        for asin in batch:
            if asin not in items and asin not in offers:
                skipped += 1
                continue
            product, rank, extra = parse_catalog_item(items.get(asin, {}))
            snap = parse_offers(offers.get(asin, {})) if asin in offers else {}
            if asin in offers:
                db.record_sellers(conn, asin, offers[asin])
            prev = db.latest_snapshot(conn, asin)
            carry = {k: prev[k] for k in _CARRY if prev and prev[k] is not None}
            if snap.get("buy_box") is None and prev:      # no Buy Box now: keep the last known price
                snap["buy_box"] = prev["buy_box"]
            db.upsert_product(conn, asin, last_checked=db.now(), **product)
            if product.get("category"):    # keep the broad category (filters group by it), replacing older sub-category values
                conn.execute("UPDATE products SET category = ? WHERE asin = ?", (product["category"], asin))
            db.add_snapshot(conn, asin, "spapi", sales_rank=rank, raw_json=json.dumps(extra), **{**carry, **snap})
            done += 1
        conn.commit()
        if i + 20 < len(asins):
            time.sleep(10)   # item-offers batch allows ~1 request per 10 seconds sustained
    return {"updated": done, "not_found": skipped}
