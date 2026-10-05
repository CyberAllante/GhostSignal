"""The loop: score every product, store a signal, alert on what changed."""

from __future__ import annotations

import json
import os
import urllib.request

from . import db, restrictions
from .scoring import Config, score_product
from .sources import marketplace_links, retailer_links

RANK = {"PASS": 0, "RESEARCH": 1, "BUY": 2}


def _enrichment(conn, asin):
    row = conn.execute("SELECT data FROM enrichment WHERE asin = ?", (asin,)).fetchone()
    return json.loads(row["data"]) if row else None


def evaluate(conn, asin: str, cfg: Config | None = None):
    product = conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()
    if product is None:
        raise KeyError(asin)
    snap = db.latest_snapshot(conn, asin)
    sources = [dict(s) for s in db.latest_sources(conn, asin)]
    orders = db.order_stats(conn, asin)
    sig = score_product(dict(product), dict(snap) if snap else None, sources, orders,
                        _enrichment(conn, asin), cfg, db.get_eligibility(conn, asin),
                        db.channel_prices(conn, asin))
    reason = restrictions.check(dict(product), restrictions.parse_list(db.get_setting(conn, restrictions.SETTING)))
    if reason:
        sig.verdict, sig.score, sig.restricted = "PASS", 0, reason
        sig.flags.insert(0, reason)
    return product, snap, sources, orders, sig


def _days_since(iso: str) -> float:
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 86400


def pass_streak_days(conn, asin: str) -> float:
    """How long this product has continuously been a PASS (0 if it isn't one)."""
    start = None
    for r in conn.execute("SELECT verdict, created_at FROM signals WHERE asin = ? ORDER BY id DESC", (asin,)):
        if r["verdict"] != "PASS":
            break
        start = r["created_at"]
    return _days_since(start) if start else 0.0


def refresh_days(verdict: str | None, score: int | None) -> float:
    """How often a product deserves a fresh look. Good ones daily, dead ones rarely,
    but never never: a product that's a PASS today can be a BUY in six months."""
    if verdict is None:
        return 0
    if verdict == "BUY":
        return 1
    if verdict == "RESEARCH":
        return 3
    return 30 if (score or 0) < 25 else 14


def record_signal(conn, asin: str, cfg: Config | None = None, force: bool = False):
    """Score one product, store a history row only when something changed (or weekly),
    and return the alert-worthy change, if any."""
    product, _, _, _, sig = evaluate(conn, asin, cfg)
    prev = db.last_signal(conn, asin)
    e = sig.economics
    # measure how long it was a PASS *before* we write today's row
    dormant = pass_streak_days(conn, asin) if prev is not None and prev["verdict"] == "PASS" else 0
    unchanged = (prev is not None and prev["verdict"] == sig.verdict and prev["score"] == sig.score
                 and prev["profit"] == e.profit and _days_since(prev["created_at"]) < 7)
    if not unchanged or force:
        conn.execute(
            """INSERT INTO signals (asin, created_at, score, verdict, profit, roi, best_source, reasons)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (asin, db.now(), sig.score, sig.verdict, e.profit, e.roi,
             sig.best_source["retailer"] if sig.best_source else None, json.dumps(sig.reasons + sig.flags)))
    change = None
    base = {"asin": asin, "title": product["title"], "signal": sig}
    if prev is None:
        if sig.verdict == "BUY":
            change = {**base, "kind": "NEW BUY"}
    elif RANK[sig.verdict] > RANK[prev["verdict"]]:
        kind = f"{prev['verdict']} → {sig.verdict}"
        if dormant >= 30:
            kind = f"REVIVED after {int(dormant)}d → {sig.verdict}"
        change = {**base, "kind": kind}
    elif sig.verdict == "BUY" and sig.score - prev["score"] >= 10:
        change = {**base, "kind": "BUY ↑"}
    return change


def run(conn, cfg: Config | None = None, statuses=("watch", "buy", "research")) -> list[dict]:
    """Score all live products. Returns the changes worth alerting on."""
    changes = []
    q = f"SELECT asin FROM products WHERE status IN ({','.join('?' * len(statuses))})"
    for (asin,) in conn.execute(q, statuses).fetchall():
        c = record_signal(conn, asin, cfg)
        if c:
            changes.append(c)
    db.set_setting(conn, "last_run", db.now())
    conn.commit()
    return changes


def recent_changes(conn, limit: int = 12, days: float | None = None) -> list[dict]:
    """Products whose latest verdict is better than the one before it."""
    rows = conn.execute(
        """SELECT s.asin, s.verdict, s.score, s.created_at, p.title FROM signals s JOIN products p ON p.asin = s.asin
           WHERE p.status != 'dead' ORDER BY s.asin, s.id DESC""").fetchall()
    by = {}
    for r in rows:
        by.setdefault(r["asin"], []).append(r)
    out = []
    for asin, sigs in by.items():
        if len(sigs) >= 2 and RANK[sigs[0]["verdict"]] > RANK[sigs[1]["verdict"]]:
            if days is not None and _days_since(sigs[0]["created_at"]) > days:
                continue
            out.append({"asin": asin, "title": sigs[0]["title"], "from": sigs[1]["verdict"], "to": sigs[0]["verdict"],
                        "score": sigs[0]["score"], "at": sigs[0]["created_at"]})
    return sorted(out, key=lambda c: c["at"], reverse=True)[:limit]


def format_alert(c: dict) -> str:
    s, e = c["signal"], c["signal"].economics
    lines = [f"🚨 {c['kind']} — {s.verdict} {s.score}/100", f"{c['title'] or c['asin']} ({c['asin']})"]
    if e.sale_price:
        lines.append(f"Sell ${e.sale_price:.2f} | Max cost ${e.max_cost:.2f}")
    if e.profit is not None:
        lines.append(f"Profit ${e.profit:.2f} | ROI {e.roi:.0%}")
    lines += [f"• {r}" for r in s.reasons]
    lines += [f"⚠ {f}" for f in s.flags]
    links = marketplace_links(c["asin"])
    lines.append(links.get("keepa") or links["ebay_sold"])
    return "\n".join(lines)


def send_alerts(changes: list[dict]) -> int:
    """Discord webhook if DISCORD_WEBHOOK_URL is set; otherwise print."""
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    for c in changes:
        msg = format_alert(c)
        if hook:
            req = urllib.request.Request(hook, data=json.dumps({"content": msg[:1900]}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "GhostSignal"})
            urllib.request.urlopen(req, timeout=20).read()
        else:
            print(msg, end="\n\n")
    return len(changes)


def product_view(conn, asin: str, cfg: Config | None = None) -> dict:
    """Everything the dashboard needs for one product card."""
    product, snap, sources, orders, sig = evaluate(conn, asin, cfg)
    e = sig.economics
    history = [dict(r) for r in conn.execute(
        "SELECT created_at, score, verdict FROM signals WHERE asin = ? ORDER BY id DESC LIMIT 30", (asin,))]
    return {
        **dict(product),
        "snapshot": dict(snap) if snap else None,
        "sources": sources,
        "orders": orders,
        "enrichment": _enrichment(conn, asin),
        "gated": sig.gated,
        "restricted": sig.restricted,
        "channel_prices": db.channel_prices(conn, asin),
        "inventory": db.inventory_rows(conn, asin),
        "eligibility": db.get_eligibility(conn, asin),
        "score": sig.score,
        "verdict": sig.verdict,
        "reasons": sig.reasons,
        "flags": sig.flags,
        "best_source": sig.best_source,
        "economics": e.__dict__,
        "links": {**marketplace_links(asin, product["title"]),
                  "retail": retailer_links(product["title"], product["upc"])},
        "history": history,
    }


_GATE_RANK = {"ungated": 3, "unknown": 2, "approval": 1, "blocked": 0}
_VERDICT_RANK = {"BUY": 2, "RESEARCH": 1, "PASS": 0}


def priority(view: dict) -> int:
    """Sort key for 'what to look at first': can-sell beats needs-approval beats blocked, then verdict, then score.
    Junk (Amazon brands, gift cards, fee lines) sinks to the bottom."""
    if view.get("restricted"):
        return -1
    return _GATE_RANK.get(view.get("gated"), 2) * 1000 + _VERDICT_RANK.get(view.get("verdict"), 0) * 100 + (view.get("score") or 0)


def list_view(conn, asin: str, cfg: Config | None = None) -> dict:
    """The slim version of product_view for lists and tables (about a tenth of the size)."""
    product, snap, _sources, orders, sig = evaluate(conn, asin, cfg)
    e = sig.economics
    s = dict(snap) if snap else None
    enr = _enrichment(conn, asin)
    el = db.get_eligibility(conn, asin)
    view = {
        "asin": asin, "title": product["title"], "brand": product["brand"], "category": product["category"],
        "image_url": product["image_url"], "status": product["status"],
        "verdict": sig.verdict, "score": sig.score, "gated": sig.gated, "restricted": sig.restricted,
        "economics": {k: getattr(e, k, None) for k in ("sale_price", "cost", "max_cost", "profit", "roi", "best_channel",
                                                         "referral_fee", "fba_fee")},
        "snapshot": {k: s.get(k) for k in ("sales_rank", "monthly_sold", "offer_count", "captured_at", "amazon_price")} if s else None,
        "orders": {"orders": orders.get("orders") or 0},
        "best_source": {"retailer": sig.best_source["retailer"]} if sig.best_source else None,
        "enrichment": {"gating_risk": enr.get("gating_risk"), "sold_in_stores": enr.get("sold_in_stores")} if enr else None,
        "eligibility": {"approval_url": el.get("approval_url"), "reason": el.get("reason"), "status": el.get("status")} if el else None,
    }
    view["priority"] = priority(view)
    return view
