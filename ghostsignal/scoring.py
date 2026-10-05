"""Economics + opportunity score.

The score is deliberately simple and explainable: every point comes with a
reason string, so the dashboard can tell you *why* something is a BUY.
Tune the weights in WEIGHTS / thresholds in Config as you learn what actually sells.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

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


@dataclass
class Economics:
    sale_price: float | None
    cost: float | None
    referral_fee: float | None
    fba_fee: float | None
    inbound: float
    net_payout: float | None          # what Amazon pays you before your cost
    profit: float | None
    roi: float | None
    margin: float | None
    max_cost: float | None            # highest buy cost that still hits target ROI
    ebay_profit: float | None = None
    best_channel: str | None = None


@dataclass
class Signal:
    score: int
    verdict: str
    economics: Economics
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    best_source: dict | None = None


def referral_rate(category: str | None, price: float, default: float = 0.15) -> float:
    """Amazon referral fee approximation. Grocery & beauty drop to 8% at <= $15."""
    c = (category or "").lower()
    if any(k in c for k in ("grocery", "gourmet", "beauty", "health", "personal care", "baby")):
        return 0.08 if price <= 15 else 0.15
    return default


def economics(sale_price, cost, category=None, referral_pct=None, fba_fee=None,
              ebay_price=None, cfg: Config | None = None) -> Economics:
    cfg = cfg or Config()
    if not sale_price:
        return Economics(None, cost, None, None, cfg.inbound_per_unit, None, None, None, None, None)
    rate = referral_pct if referral_pct else referral_rate(category, sale_price, cfg.default_referral)
    ref = round(sale_price * rate, 2)
    fba = fba_fee if fba_fee is not None else cfg.default_fba_fee
    net = round(sale_price - ref - fba - cfg.inbound_per_unit, 2)
    max_cost = round(net / (1 + cfg.target_roi), 2) if net > 0 else 0.0
    profit = roi = margin = None
    if cost:
        profit = round(net - cost, 2)
        roi = round(profit / cost, 3)
        margin = round(profit / sale_price, 3)

    ebay_profit = best = None
    if ebay_price and cost:
        ebay_profit = round(ebay_price * (1 - cfg.ebay_fee_pct) - cfg.ebay_fixed - cfg.ebay_ship - cost, 2)
    if profit is not None:
        best = "ebay" if ebay_profit is not None and ebay_profit > profit else "amazon"
    return Economics(sale_price, cost, ref, fba, cfg.inbound_per_unit, net, profit, roi, margin,
                     max_cost, ebay_profit, best)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_product(product: dict, snapshot: dict | None, sources: list[dict], orders: dict,
                  enrichment: dict | None = None, cfg: Config | None = None) -> Signal:
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
        unit_cost = s["price"] * (s.get("pack_qty") or 1)
        if best is None or unit_cost < best["unit_cost"]:
            best = {**s, "unit_cost": round(unit_cost, 2)}
    cost = best["unit_cost"] if best else None

    econ = economics(sale, cost, product.get("category"), snap.get("referral_pct"),
                     snap.get("fba_fee"), snap.get("ebay_sold_price"), cfg)

    # --- profit / ROI ---
    best_profit = max(p for p in (econ.profit, econ.ebay_profit, -999) if p is not None)
    if econ.profit is not None:
        pts["profit"] = _clamp(best_profit / 10) * WEIGHTS["profit"]
        pts["roi"] = _clamp((econ.roi or 0) / 1.0) * WEIGHTS["roi"]
        reasons.append(f"Est. profit ${best_profit:.2f}/unit, ROI {econ.roi:.0%}"
                       + (f" (best on {econ.best_channel})" if econ.best_channel == "ebay" else ""))
    elif econ.max_cost:
        flags.append(f"No retail cost yet — buy under ${econ.max_cost:.2f} for {cfg.target_roi:.0%} ROI")

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

    # --- AI enrichment risk flags ---
    e = enrichment or {}
    penalty = 0
    if e.get("gating_risk") == "high":
        flags.append("Likely gated / brand-restricted — check eligibility")
        penalty += 15
    if e.get("hazmat_risk") == "high":
        flags.append("Possible hazmat")
        penalty += 10
    if e.get("ip_complaint_risk") == "high":
        flags.append("Brand known for IP complaints")
        penalty += 15
    if e.get("replenishable"):
        reasons.append("Replenishable / repeat-purchase item")

    score = int(round(max(0, sum(pts.values()) - penalty)))

    if (econ.profit is not None and score >= cfg.buy_score and best_profit >= cfg.min_profit_buy
            and max(econ.roi or 0, (econ.ebay_profit or 0) / cost if cost else 0) >= cfg.min_roi_buy):
        verdict = "BUY"
    elif econ.profit is not None and best_profit < 1:
        verdict = "PASS"
        flags.append("Margin too thin at current cost")
    elif score >= cfg.research_score or econ.profit is None:
        verdict = "RESEARCH"
    else:
        verdict = "PASS"

    return Signal(score, verdict, econ, reasons, flags, best)
