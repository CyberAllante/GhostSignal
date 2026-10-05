"""Amazon Selling Partner API (SP-API): the official, free API for your seller account.

What we use it for:
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
    return "blocked", msg, url


def check_gated(conn, asins: list[str]) -> dict:
    counts = {"ungated": 0, "approval": 0, "blocked": 0}
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
