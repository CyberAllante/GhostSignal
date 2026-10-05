"""The loop: score every product, store a signal, alert on what changed."""

from __future__ import annotations

import json
import os
import urllib.request

from . import db
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
    return product, snap, sources, orders, sig


def run(conn, cfg: Config | None = None, statuses=("watch", "buy", "research")) -> list[dict]:
    """Score all live products. Returns the list of *changes* worth alerting on."""
    changes = []
    q = f"SELECT asin FROM products WHERE status IN ({','.join('?' * len(statuses))})"
    for (asin,) in conn.execute(q, statuses).fetchall():
        product, _, _, _, sig = evaluate(conn, asin, cfg)
        prev = db.last_signal(conn, asin)
        conn.execute(
            """INSERT INTO signals (asin, created_at, score, verdict, profit, roi, best_source, reasons)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (asin, db.now(), sig.score, sig.verdict, sig.economics.profit, sig.economics.roi,
             sig.best_source["retailer"] if sig.best_source else None,
             json.dumps(sig.reasons + sig.flags)),
        )
        if prev is None:
            if sig.verdict == "BUY":
                changes.append({"asin": asin, "title": product["title"], "kind": "NEW BUY", "signal": sig})
        elif RANK[sig.verdict] > RANK[prev["verdict"]]:
            changes.append({"asin": asin, "title": product["title"],
                            "kind": f"{prev['verdict']} → {sig.verdict}", "signal": sig})
        elif sig.verdict == "BUY" and sig.score - prev["score"] >= 10:
            changes.append({"asin": asin, "title": product["title"], "kind": "BUY ↑", "signal": sig})
    conn.commit()
    return changes


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
