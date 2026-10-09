"""Economics + opportunity score.

The score is deliberately simple and explainable: every point comes with a
reason string, so the dashboard can tell you *why* something is a BUY.
Tune the weights in WEIGHTS / thresholds in Config as you learn what actually sells.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


# Brands written in capitals that are real retail brands (so the knockoff check below leaves them alone).
CAPS_OK = {"CRAYOLA", "DEWALT", "STANLEY", "HASBRO", "MATTEL", "PURINA", "CELSIUS", "NIVEA", "CETAPHIL", "SHARPIE",
           "GREENIES", "MCCORMICK", "KIRKLAND", "RUBBERMAID", "PYREX", "FUNKO", "POKEMON", "BISSELL", "SCOTCH", "NERF",
           "LEVOIT", "CRAFTSMAN", "RYOBI", "MAKITA", "BLACK+DECKER", "HUSKY", "COLEMAN", "IGLOO", "YETI", "STANLEY",
           "OLAPLEX", "CERAVE", "NEUTROGENA", "REVLON", "MAYBELLINE", "GILLETTE", "COLGATE", "CREST", "TIDE", "LYSOL",
           "CLOROX", "GLAD", "HEFTY", "ZIPLOC", "DIXIE", "BOUNTY", "CHARMIN", "DURACELL", "ENERGIZER", "SAMSUNG", "SONY"}


# Brands that sell mainly on Amazon (Walmart.com hits for them are marketplace resellers, not the store).
AMAZON_FIRST = {"utopia bedding", "utopia kitchen", "utopia towels", "usx mount", "auvon", "amazon basics", "amazonbasics",
                "amazon essentials", "solimo", "happy belly", "presto!", "mountup", "pipishell", "mounting dream",
                "beckham hotel collection", "bedsure", "amazon commercial", "wag", "goodthreads"}


def looks_import(brand: str | None, title: str | None, enr: dict | None = None) -> bool:
    """Chinese private label / Amazon-first brand you can't walk into a store and buy. The AI's brand_type wins when
    present; otherwise a few hard tells: no brand or 'Generic', 'for <Brand> ...' replacement parts, and invented
    all-caps letter strings (SHISHUVIN, IEADEN)."""
    bt = (enr or {}).get("brand_type")
    if bt in ("national", "store_brand"):
        return False
    if bt in ("amazon_first", "generic_import"):
        return True
    b = (brand or "").strip()
    if b.lower() in AMAZON_FIRST:
        return True
    if b.lower() in ("generic", "unbranded", "n/a"):      # a missing brand just means market data hasn't run yet
        return True
    t = (title or "").strip().lower()
    if t.startswith(("for ", "compatible with", "replacement for")):
        return True
    return bool(re.fullmatch(r"[A-Z]{5,}", b)) and b not in CAPS_OK

# Max points per component (sum = 100).
WEIGHTS = {
    "profit": 22,
    "roi": 18,
    "demand": 20,
    "competition": 12,
    "crowd": 12,       # your opted-in order-history dataset
    "availability": 8,
    "stability": 8,
}


@dataclass
class Config:
    target_roi: float = 0.30          # used for Max Cost
    min_profit_buy: float = 3.00
    min_roi_buy: float = 0.30
    buy_score: int = 70
    research_score: int = 45
    inbound_per_unit: float = 0.60    # ship-to-Amazon + prep, per unit
    default_fba_fee: float = 3.75
    default_referral: float = 0.15
    ebay_fee_pct: float = 0.136
    ebay_fixed: float = 0.40
    ebay_ship: float = 6.50
    facebook_fee_pct: float = 0.0     # local pickup is free; set 0.10 if you ship


@dataclass
class Economics:
    sale_price: float | None          # Amazon price
    cost: float | None
    referral_fee: float | None
    fba_fee: float | None
    inbound: float
    net_payout: float | None          # what Amazon pays you before your cost
    profit: float | None              # best channel
    roi: float | None                 # best channel
    margin: float | None
    max_cost: float | None            # highest buy cost that still hits target ROI on the best channel
    ebay_profit: float | None = None
    best_channel: str | None = None   # amazon | ebay | facebook
    facebook_profit: float | None = None
    channels: dict = field(default_factory=dict)   # per channel: price, net, profit, roi
    fbm: dict | None = None           # ship it yourself: label, net, profit, roi, max_cost (needs weight)


# Shipping label by package weight for FBM, from the Seller Syndicate playbook's commercial-rate table
# (upper-middle of each range, to stay conservative). Size over ~14" on a side costs more; not modelled.
FBM_LABEL_TIERS = [(0.25, 6.75), (0.5, 7.50), (0.75, 8.25), (1.0, 9.50), (2.0, 9.50), (5.0, 11.00), (10.0, 13.00)]


def fbm_label_cost(weight_lb: float | None) -> float | None:
    if not weight_lb:
        return None
    for limit, cost in FBM_LABEL_TIERS:
        if weight_lb <= limit:
            return cost
    return round(14.0 + 0.6 * (weight_lb - 10), 2)


@dataclass
class Signal:
    score: int
    verdict: str
    economics: Economics
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    best_source: dict | None = None
    gated: str = "unknown"            # ungated | approval | blocked | unknown
    restricted: str = ""              # reason this can never be resold (Amazon brand, your blocklist)


def referral_rate(category: str | None, price: float, default: float = 0.15) -> float:
    """Amazon referral fee approximation. Grocery & beauty drop to 8% at <= $15."""
    c = (category or "").lower()
    if any(k in c for k in ("grocery", "gourmet", "beauty", "health", "personal care", "baby")):
        return 0.08 if price <= 15 else 0.15
    return default


def economics(sale_price, cost, category=None, referral_pct=None, fba_fee=None,
              ebay_price=None, cfg: Config | None = None, facebook_price=None,
              exclude: tuple = (), weight_lb: float | None = None) -> Economics:
    """Profit on every channel we have a price for; headline numbers = the best channel."""
    cfg = cfg or Config()
    channels: dict[str, dict] = {}
    ref = fba = amz_net = None
    if sale_price:
        rate = referral_pct if referral_pct else referral_rate(category, sale_price, cfg.default_referral)
        ref = round(sale_price * rate, 2)
        fba = fba_fee if fba_fee is not None else cfg.default_fba_fee
        amz_net = round(sale_price - ref - fba - cfg.inbound_per_unit, 2)
        channels["amazon"] = {"price": sale_price, "net": amz_net}
    if ebay_price:
        channels["ebay"] = {"price": ebay_price, "net": round(
            ebay_price * (1 - cfg.ebay_fee_pct) - cfg.ebay_fixed - cfg.ebay_ship, 2)}
    if facebook_price:
        channels["facebook"] = {"price": facebook_price, "net": round(facebook_price * (1 - cfg.facebook_fee_pct), 2)}
    for c in channels.values():
        c["profit"] = round(c["net"] - cost, 2) if cost else None
        c["roi"] = round(c["profit"] / cost, 3) if cost else None

    usable = {k: v for k, v in channels.items() if k not in exclude}
    best = max(usable, key=lambda k: usable[k]["net"]) if usable else None
    b = usable.get(best, {})
    best_net = b.get("net")
    max_cost = round(best_net / (1 + cfg.target_roi), 2) if best_net and best_net > 0 else (0.0 if best else None)
    profit, roi = b.get("profit"), b.get("roi")
    margin = round(profit / b["price"], 3) if profit is not None else None
    fbm = None
    label = fbm_label_cost(weight_lb)
    if sale_price and label is not None and "amazon" not in exclude:
        net = round(sale_price - ref - label, 2)
        fbm = {"label": label, "net": net,
               "profit": round(net - cost, 2) if cost else None,
               "roi": round((net - cost) / cost, 3) if cost else None,
               "max_cost": round(net / (1 + cfg.target_roi), 2) if net > 0 else 0.0}
    return Economics(sale_price, cost, ref, fba, cfg.inbound_per_unit, amz_net, profit, roi, margin, max_cost,
                     channels.get("ebay", {}).get("profit"), best,
                     channels.get("facebook", {}).get("profit"), channels, fbm)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_product(product: dict, snapshot: dict | None, sources: list[dict], orders: dict,
                  enrichment: dict | None = None, cfg: Config | None = None,
                  eligibility: dict | None = None, channel_prices: dict | None = None) -> Signal:
    cfg = cfg or Config()
    snap = snapshot or {}
    reasons: list[str] = []
    flags: list[str] = []
    pts: dict[str, float] = {}

    sale = snap.get("buy_box") or snap.get("avg_price_90")
    if not snap.get("buy_box") and sale:
        flags.append("No current Buy Box — using 90d average")

    # Cheapest per-Amazon-unit retail cost that is not known to be out of stock.
    best = None
    for s in sources:
        if s.get("price") is None or s.get("in_stock") == 0:
            continue
        note = s.get("note") or ""
        if note.startswith("similar item"):            # shown for reference only, never a source
            continue
        unit_cost = s["price"] * (s.get("pack_qty") or 1)
        if "size not stated" in note and sale and unit_cost < sale * 0.35:
            continue      # an unconfirmed size at a third of Amazon's price is a smaller pack, not a deal
        if best is None or unit_cost < best["unit_cost"]:
            best = {**s, "unit_cost": round(unit_cost, 2)}
    cost = best["unit_cost"] if best else None

    cp = channel_prices or {}
    gated = (eligibility or {}).get("status") or "unknown"
    econ = economics(sale, cost, product.get("category"), snap.get("referral_pct"), snap.get("fba_fee"),
                     cp.get("ebay") or snap.get("ebay_sold_price"), cfg, cp.get("facebook"),
                     exclude=("amazon",) if gated in ("blocked", "limited") else (),
                     weight_lb=product.get("weight_lb"))

    # --- profit / ROI ---
    NAMES = {"amazon": "Amazon", "ebay": "eBay", "facebook": "Facebook Marketplace"}
    best_profit = econ.profit if econ.profit is not None else -999
    if econ.profit is not None:
        pts["profit"] = _clamp(best_profit / 10) * WEIGHTS["profit"]
        pts["roi"] = _clamp((econ.roi or 0) / 1.0) * WEIGHTS["roi"]
        reasons.append(f"Est. profit ${best_profit:.2f}/unit, ROI {econ.roi:.0%} on {NAMES[econ.best_channel]}")
        others = [f"{NAMES[k]} ${v['profit']:.2f}" for k, v in econ.channels.items()
                  if k != econ.best_channel and v.get("profit") is not None]
        if others:
            reasons.append("Also: " + ", ".join(others))
    elif econ.max_cost:
        flags.append(f"No retail cost yet — buy under ${econ.max_cost:.2f} for {cfg.target_roi:.0%} ROI")

    if econ.fbm and econ.fbm["max_cost"] < 3 and sale:
        flags.append(f"Too cheap to ship yourself: a ~${econ.fbm['label']:.2f} label leaves ${econ.fbm['net']:.2f} (playbook: FBM items $20+)")

    # --- demand ---
    ms, rank = snap.get("monthly_sold"), snap.get("sales_rank")
    if ms:
        pts["demand"] = _clamp(math.log10(ms) / 3) * WEIGHTS["demand"]   # 1000+/mo = full marks
        reasons.append(f"~{ms:,} sold/month")
    elif rank:
        pts["demand"] = _clamp(1 - math.log10(max(rank, 1)) / 6) * WEIGHTS["demand"]  # rank 1 → full, 1M → 0
        reasons.append(f"Sales rank #{rank:,}")
    else:
        flags.append("No demand data")

    # --- competition ---
    offers = snap.get("offer_count")
    if offers is not None:
        c = _clamp(1 - offers / 30)
        if snap.get("amazon_price"):
            c *= 0.4
            flags.append("Amazon is on the listing")
        pts["competition"] = c * WEIGHTS["competition"]
        reasons.append(f"{offers} offers")

    # --- crowd (opted-in order history) ---
    if orders.get("orders"):
        crowd = _clamp(orders["buyers"] / 5) * 0.6 + _clamp(orders["repeat_buyers"] / 3) * 0.4
        pts["crowd"] = crowd * WEIGHTS["crowd"]
        reasons.append(f"Bought {orders['orders']}x by {orders['buyers']} people in your network"
                       + (f", {orders['repeat_buyers']} repeat" if orders["repeat_buyers"] else ""))

    # --- availability ---
    if best:
        pts["availability"] = (1.0 if best.get("in_stock") == 1 else 0.6) * WEIGHTS["availability"]
        reasons.append(f"Source: {best['retailer']} @ ${best['price']:.2f}"
                       + (f" x{best['pack_qty']}" if (best.get("pack_qty") or 1) > 1 else "")
                       + (f" ({best['promo']})" if best.get("promo") else ""))

    # --- price stability: current vs 90d average ---
    bb, avg = snap.get("buy_box"), snap.get("avg_price_90")
    if bb and avg:
        drift = (bb - avg) / avg
        pts["stability"] = _clamp(1 - abs(drift) / 0.3) * WEIGHTS["stability"]
        if drift < -0.15:
            flags.append(f"Price {abs(drift):.0%} below 90d avg — possible race to the bottom")
        elif drift > 0.15:
            flags.append(f"Price {drift:.0%} above 90d avg — may be a temporary spike")

    # --- Can you actually sell it? Confirmed status (Amazon SP-API or you) beats the AI guess. ---
    e = enrichment or {}
    penalty = 0
    if gated == "ungated":
        reasons.append("You can sell this (ungated)")
    elif gated == "approval":
        flags.append("Gated — you need approval to sell this")
        penalty += 10
    elif gated == "blocked":
        flags.append("You can't sell this on Amazon (brand closed to new sellers)" + (" — eBay/Facebook only" if econ.best_channel else ""))
    elif gated == "limited":
        flags.append("Not eligible on Amazon yet — needs approval and your account has listing limits; usually opens up with sales history"
                     + (" — eBay/Facebook for now" if econ.best_channel else ""))
    elif e.get("gating_risk") == "high":
        flags.append("Likely gated — check eligibility before buying")
        penalty += 15
    elif e.get("gating_risk") == "medium":
        flags.append("Might be gated — check eligibility")
        penalty += 5
    if e.get("hazmat_risk") == "high":
        flags.append("Possible hazmat")
        penalty += 10
    if e.get("ip_complaint_risk") == "high":
        flags.append("Brand known for IP complaints")
        penalty += 15
    resellers_only = str(product.get("store_note") or "").startswith("resellers_only")
    import_brand = looks_import(product.get("brand"), product.get("title"), e)
    amazon_only = (e.get("sold_in_stores") is False or resellers_only or import_brand) and best is None
    if resellers_only:
        flags.append("Store check found only eBay/Mercari resellers: no retail store carries this")
    if amazon_only:
        flags.append("No store sells this (Amazon-only or knockoff import brand): nothing to buy for resale")
        penalty += 25
    if e.get("replenishable"):
        reasons.append("Replenishable / repeat-purchase item")

    score = int(round(max(0, sum(pts.values()) - penalty)))

    if (econ.profit is not None and score >= cfg.buy_score and best_profit >= cfg.min_profit_buy
            and (econ.roi or 0) >= cfg.min_roi_buy):
        verdict = "BUY"
    elif econ.profit is not None and best_profit < 1:
        verdict = "PASS"
        flags.append("Margin too thin at current cost")
    elif score >= cfg.research_score or econ.profit is None:
        verdict = "RESEARCH"
    else:
        verdict = "PASS"

    if amazon_only and verdict != "PASS":
        verdict = "PASS"
    # A store price whose size we couldn't confirm can't make a BUY on its own.
    if best and "may be old" in (best.get("note") or ""):
        flags.append(f"Store price at {best['retailer']} comes from a barcode database and may be old: check the current price before buying")
        if verdict == "BUY":
            verdict = "RESEARCH"
    if best and "size not stated" in (best.get("note") or ""):
        flags.append(f"Store price at {best['retailer']} is unverified (size not stated): confirm it's the same size before buying")
        if verdict == "BUY":
            verdict = "RESEARCH"
    # Gating overrides: never tell you to BUY something you can't list.
    if gated in ("blocked", "limited") and econ.best_channel is None:
        verdict = "PASS"
    elif gated == "approval" and verdict == "BUY" and econ.best_channel == "amazon":
        verdict = "RESEARCH"

    return Signal(score, verdict, econ, reasons, flags, best, gated)
