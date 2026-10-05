"""MCP server: lets Claude (or any MCP client) run GhostSignal for you.

Served by the same web service at /mcp (Streamable HTTP, JSON-RPC over POST).
No extra dependencies. Auth is the same GHOSTSIGNAL_PASSWORD, sent as a Bearer token.

Design: the agent does the processing; you only look at the picks. Tool results are
short plain text (one line per product) so a conversation over thousands of products
stays cheap. Anything the agent changes is re-scored immediately.
"""

from __future__ import annotations

import json
import os

from . import __version__, db, engine, importers

PROTOCOL = "2025-03-26"

INSTRUCTIONS = """GhostSignal is a master product database for retail arbitrage (sell on Amazon, eBay, Facebook Marketplace).
You are the analyst; the user only wants to see what you picked. Work like this:
1. `status` first: it shows counts, the last run, and what's connected.
2. `picks` = products worth buying now. `what_changed` = products that just got better (including old PASSes that revived).
3. `todo` lists products you can't judge yet (no price data, no store price, gating unknown). Fill gaps with your
   own research (`log_price`, `set_sell_price`, `set_snapshot`, `set_gated`), or ask the user a short question.
4. Never treat PASS as final. Prices and competition change; PASSes are re-checked on a slow schedule.
5. Use `note` to remember findings (pack sizes, brand quirks) so you don't redo research.
6. Keep the list clean: `cleanup` previews junk (Amazon brands, gift cards, cannot-sell, PASS); archive on request.\n   `outcomes` shows whether BUY verdicts really made money.\n7. Prices from automatic lookups can be the wrong pack size. Verify before calling something a BUY.
Verdicts: BUY = strong and verified; RESEARCH = promising, something unverified; PASS = not now."""

VERDICTS = ["BUY", "RESEARCH", "PASS"]
STATUSES = ["watch", "buy", "research", "pass", "dead"]
CHANNELS = ["amazon", "ebay", "facebook"]


def _money(x):
    return "-" if x is None else (f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}")


def _line(v: dict) -> str:
    e, s = v["economics"], v["snapshot"] or {}
    ch = f" via {e['best_channel']}" if e.get("best_channel") else ""
    roi = f"{e['roi']:.0%}" if e.get("roi") is not None else "-"
    gate = {"ungated": "can sell", "approval": "NEEDS APPROVAL", "blocked": "CAN'T SELL"}.get(v["gated"], "gating unchecked")
    return (f"{v['asin']} | {v['verdict']} {v['score']} | {(v['title'] or '')[:70]} | sell {_money(e['sale_price'])} "
            f"cost {_money(e['cost'])} profit {_money(e['profit'])}{ch} ROI {roi} | "
            f"{s.get('monthly_sold') or '-'}/mo | {(v['best_source'] or {}).get('retailer') or 'no store yet'} | {gate}")


def _latest(conn, where="", args=(), order="s.score DESC", limit=20):
    """Products joined to their most recent signal (cheap for thousands of rows)."""
    return conn.execute(
        f"""SELECT p.asin, p.title, p.brand, p.status, p.notes, s.verdict, s.score, s.profit, s.roi, s.best_source, s.created_at
            FROM products p JOIN signals s ON s.id = (SELECT MAX(id) FROM signals WHERE asin = p.asin)
            WHERE p.status != 'dead' {where} ORDER BY {order} LIMIT ?""", (*args, limit)).fetchall()


def _view_lines(conn, rows) -> str:
    if not rows:
        return "None."
    return "\n".join(_line(engine.product_view(conn, r["asin"])) for r in rows)


def _path(conn) -> str:
    """File path of this connection's database, so background work uses the same one."""
    return conn.execute("PRAGMA database_list").fetchone()["file"]


def _rescore(conn, asin: str) -> str:
    asin = asin.strip().upper()
    engine.record_signal(conn, asin)
    conn.commit()
    return _line(engine.product_view(conn, asin))


# ---------------- tools ----------------

def t_status(conn, a):
    from .server import is_running
    from .setup_status import checklist

    def n(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    latest = ("SELECT COUNT(*) FROM signals s JOIN products p ON p.asin = s.asin WHERE p.status != 'dead' "
              "AND s.id = (SELECT MAX(id) FROM signals WHERE asin = p.asin) AND s.verdict = ?")
    counts = {v: n(latest, v) for v in VERDICTS}
    total = n("SELECT COUNT(*) FROM products WHERE status != 'dead'")
    unscored = n("SELECT COUNT(*) FROM products p WHERE NOT EXISTS (SELECT 1 FROM signals WHERE asin = p.asin)")
    items = checklist(conn)
    running = " (running now)" if is_running() else ""
    return "\n".join([
        f"Products: {total} (BUY {counts['BUY']}, RESEARCH {counts['RESEARCH']}, PASS {counts['PASS']}, unscored {unscored})",
        f"Orders imported: {n('SELECT COUNT(*) FROM orders')} lines from {n('SELECT COUNT(DISTINCT buyer) FROM orders')} people",
        f"Last run: {db.get_setting(conn, 'last_run') or 'never'}{running}",
        "Connected: " + (", ".join(i["label"] for i in items if i["done"]) or "nothing yet"),
        "Not connected: " + (", ".join(i["label"] for i in items if not i["done"]) or "nothing"),
    ])


def t_picks(conn, a):
    v = (a.get("verdict") or "").upper()
    where, args = ("AND s.verdict = ?", (v,)) if v in VERDICTS else ("AND s.verdict IN ('BUY','RESEARCH')", ())
    if a.get("min_score"):
        where += " AND s.score >= ?"
        args += (int(a["min_score"]),)
    rows = _latest(conn, where, args, "CASE s.verdict WHEN 'BUY' THEN 0 ELSE 1 END, s.score DESC", int(a.get("limit") or 10))
    return _view_lines(conn, rows)


def t_what_changed(conn, a):
    days = float(a.get("days") or 7)
    ch = engine.recent_changes(conn, 25, days)
    if not ch:
        return f"Nothing improved in the last {days:g} days."
    lines = []
    for c in ch:
        lines.append(f"{c['asin']} | {c['from']} -> {c['to']} {c['score']} | {(c['title'] or '')[:70]} | {c['at'][:10]}")
    return "\n".join(lines)


def t_search(conn, a):
    q = f"%{(a.get('query') or '').strip().lower()}%"
    v = (a.get("verdict") or "").upper()
    where = "AND (LOWER(p.title) LIKE ? OR LOWER(p.brand) LIKE ? OR LOWER(p.asin) LIKE ?)" + (" AND s.verdict = ?" if v in VERDICTS else "")
    args = (q, q, q) + ((v,) if v in VERDICTS else ())
    return _view_lines(conn, _latest(conn, where, args, "s.score DESC", int(a.get("limit") or 10)))


def t_product(conn, a):
    asin = a["asin"].strip().upper()
    v = engine.product_view(conn, asin)
    e, s = v["economics"], v["snapshot"] or {}
    out = [_line(v), f"Brand: {v['brand'] or '-'} | Category: {v['category'] or '-'} | Status: {v['status']} | Origin: {v['origin'] or '-'}"]
    out.append("Amazon: " + (f"buy box {_money(s.get('buy_box'))}, 90d avg {_money(s.get('avg_price_90'))}, rank {s.get('sales_rank') or '-'}, "
                              f"{s.get('offer_count') or '-'} offers, fees {_money((e['referral_fee'] or 0) + (e['fba_fee'] or 0) or None)}"
                              if s else "no data yet"))
    ch = e.get("channels") or {}
    out.append("Channels: " + (", ".join(f"{k} {_money(c['price'])} (profit {_money(c['profit'])})" for k, c in ch.items()) or "none"))
    if v["sources"]:
        out.append("Stores: " + "; ".join(f"{x['retailer']} {_money(x['price'])}" + (f" x{x['pack_qty']}" if x['pack_qty'] > 1 else "")
                                           + (f" [{x['promo']}]" if x['promo'] else "") + (f" ({x['note'][:60]})" if x['note'] else "")
                                           for x in v["sources"]))
    o = v["orders"]
    if o["orders"]:
        out.append(f"Network: bought {o['orders']}x by {o['buyers']} people, {o['repeat_buyers']} repeat, last {o['last_order'] or '-'}")
    out += [f"+ {r}" for r in v["reasons"]] + [f"! {f}" for f in v["flags"]]
    if v["enrichment"]:
        out.append(f"AI: {v['enrichment'].get('notes', '')} (gating {v['enrichment'].get('gating_risk')}, hazmat {v['enrichment'].get('hazmat_risk')})")
    if v["inventory"]:
        out.append("Inventory: " + "; ".join(f"{i['qty']}x @ {_money(i['unit_cost'])} {i['status']}{' on ' + i['channel'] if i['channel'] else ''}" for i in v["inventory"]))
    hist = [f"{h['created_at'][:10]} {h['verdict']} {h['score']}" for h in v["history"][:8]]
    out.append("History: " + (", ".join(hist) or "none"))
    p = conn.execute("SELECT notes FROM products WHERE asin = ?", (asin,)).fetchone()
    if p and p["notes"]:
        out.append("Notes:\n" + p["notes"])
    out.append(f"Links: amazon.com/dp/{asin}" if not asin.startswith("GS") else "Not an Amazon product")
    return "\n".join(out)


def t_todo(conn, a):
    lim = int(a.get("limit") or 15)
    q = lambda sql: [r["asin"] for r in conn.execute(sql + " LIMIT ?", (lim,))]  # noqa: E731
    base = "SELECT p.asin FROM products p WHERE p.status NOT IN ('dead','pass')"
    no_data = q(base + " AND NOT EXISTS (SELECT 1 FROM snapshots WHERE asin = p.asin)")
    no_store = q(base + " AND EXISTS (SELECT 1 FROM snapshots WHERE asin = p.asin) AND NOT EXISTS (SELECT 1 FROM retail_sources WHERE asin = p.asin)")
    gate = q("SELECT p.asin FROM products p JOIN signals s ON s.id = (SELECT MAX(id) FROM signals WHERE asin = p.asin) "
             "WHERE p.status NOT IN ('dead','pass') AND s.verdict IN ('BUY','RESEARCH') AND p.asin NOT LIKE 'GS%' "
             "AND NOT EXISTS (SELECT 1 FROM eligibility WHERE asin = p.asin) ORDER BY s.score DESC")
    t = lambda xs: ", ".join(xs) if xs else "none"  # noqa: E731
    cnt = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return "\n".join([
        f"No marketplace data (needs Keepa or log it with set_snapshot): {cnt('SELECT COUNT(*) FROM (' + base + ' AND NOT EXISTS (SELECT 1 FROM snapshots WHERE asin = p.asin))')} products. First: {t(no_data)}",
        f"Has Amazon data but no store price (use log_price or the store lookup): first {t(no_store)}",
        f"Promising but gating unchecked (set_gated): {t(gate)}",
    ])


def t_add_products(conn, a):
    n = importers.import_asin_text(conn, a["text"], a.get("origin") or "agent")
    for (asin,) in conn.execute("SELECT asin FROM products p WHERE NOT EXISTS (SELECT 1 FROM signals WHERE asin = p.asin)").fetchall():
        engine.record_signal(conn, asin)
    conn.commit()
    return f"{n} ASINs found/added."


def t_import_orders(conn, a):
    import re
    import tempfile
    from pathlib import Path
    name = a.get("filename") or "orders.json"
    buyer = (a.get("buyer") or "").strip()
    if not buyer:
        m = re.match(r"amazon-orders-(.+?)(?:\s\(\d+\))?\.(?:json|csv)$", name, re.I)
        buyer = m.group(1) if m else "me"
    with tempfile.NamedTemporaryFile("w", suffix=Path(name).suffix or ".json", delete=False, encoding="utf-8") as f:
        f.write(a["content"])
    try:
        r = importers.import_orders(conn, f.name, buyer)
    finally:
        Path(f.name).unlink(missing_ok=True)
    from .server import refresh_in_background
    refresh_in_background(_path(conn))
    return f"Imported {r['added']} order lines for '{buyer}' ({r['skipped']} skipped). Looking up data in the background; call status in a minute."


def t_log_price(conn, a):
    db.upsert_product(conn, a["asin"])
    db.add_source(conn, a["asin"], a["retailer"], float(a["price"]), pack_qty=int(a.get("pack_qty") or 1),
                  promo=a.get("promo"), url=a.get("url"), in_stock=a.get("in_stock"), note=a.get("note") or "")
    return _rescore(conn, a["asin"])


def t_set_sell_price(conn, a):
    db.set_channel_price(conn, a["asin"], a["channel"], float(a["price"]))
    return _rescore(conn, a["asin"])


def t_set_snapshot(conn, a):
    fields = {k: a[k] for k in ("buy_box", "avg_price_90", "sales_rank", "monthly_sold", "offer_count",
                                "amazon_price", "ebay_sold_price", "fba_fee") if a.get(k) is not None}
    db.upsert_product(conn, a["asin"], title=a.get("title"), brand=a.get("brand"), category=a.get("category"))
    db.add_snapshot(conn, a["asin"], "agent", **fields)
    return _rescore(conn, a["asin"])


def t_set_gated(conn, a):
    db.set_eligibility(conn, a["asin"], a["status"], "agent", a.get("reason") or "")
    return _rescore(conn, a["asin"])


def t_set_status(conn, a):
    conn.execute("UPDATE products SET status = ? WHERE asin = ?", (a["status"], a["asin"].strip().upper()))
    conn.commit()
    return f"{a['asin'].upper()} -> {a['status']}"


def t_note(conn, a):
    asin = a["asin"].strip().upper()
    row = conn.execute("SELECT notes FROM products WHERE asin = ?", (asin,)).fetchone()
    if row is None:
        return f"Unknown product {asin}"
    new = (row["notes"] + "\n" if row["notes"] else "") + f"[{db.now()[:10]}] {a['text'].strip()}"
    conn.execute("UPDATE products SET notes = ? WHERE asin = ?", (new, asin))
    conn.commit()
    return "Noted."


def t_log_purchase(conn, a):
    db.add_inventory(conn, a["asin"], int(a.get("qty") or 1), float(a["unit_cost"]) if a.get("unit_cost") is not None else None,
                     a.get("store") or "", a.get("channel") or "", float(a["list_price"]) if a.get("list_price") is not None else None)
    conn.commit()
    return f"Logged {a.get('qty') or 1} x {a['asin'].upper()} in inventory."


def t_inventory(conn, a):
    rows = db.inventory_rows(conn)
    if not rows:
        return "Inventory is empty."
    return "\n".join(f"#{r['id']} {r['asin']} | {(r['title'] or '')[:50]} | {r['qty']}x @ {_money(r['unit_cost'])} | {r['channel'] or 'not listed'} | "
                     f"{r['status']}" + (f" | profit {_money(r['profit'])}" if r["profit"] is not None else "") for r in rows[:50])


def t_mark_sold(conn, a):
    db.update_inventory(conn, int(a["id"]), status="sold", sold_price=float(a["sold_price"]))
    conn.commit()
    return "Marked sold."


def t_track_seller(conn, a):
    sid = importers.parse_seller_id(a["seller"])
    if not sid:
        return "Couldn't find a seller ID in that. Give the storefront URL (has seller=A...) or the ID."
    db.add_seller(conn, sid, a.get("name"))
    conn.commit()
    msg = f"Tracking {sid}."
    if os.environ.get("KEEPA_API_KEY"):
        from . import keepa
        msg += f" Pulled {len(keepa.seller_storefront(conn, sid))} products."
    else:
        msg += " Needs KEEPA_API_KEY to pull their products."
    return msg


def t_run_now(conn, a):
    from .server import is_running, refresh_in_background
    if is_running():
        return "A run is already in progress."
    refresh_in_background(_path(conn))
    return "Started: refreshing data, store prices, gating and scores for whatever is connected. Check status in a few minutes."


def _live_views(conn):
    return [engine.product_view(conn, r[0]) for r in conn.execute("SELECT asin FROM products WHERE status != 'dead'")]


_GROUPS = {
    "restricted": lambda v: bool(v["restricted"]),
    "cant_sell": lambda v: v["gated"] == "blocked" and not v["economics"].get("best_channel") and not v["restricted"],
    "pass": lambda v: v["verdict"] == "PASS",
}


def t_cleanup(conn, a):
    """Preview (default) or archive junk: Amazon-owned brands/gift cards/your blocklist, cannot-sell, all PASS."""
    views = _live_views(conn)
    groups = a.get("groups") or []
    out = []
    for name, f in _GROUPS.items():
        hits = [v for v in views if f(v)]
        out.append(f"{name}: {len(hits)}" + ("" if not hits else " e.g. " + "; ".join((v["title"] or v["asin"])[:40] for v in hits[:3])))
    if a.get("archive") and groups:
        asins = {v["asin"] for v in views if any(_GROUPS[g](v) for g in groups if g in _GROUPS)}
        n = db.archive_products(conn, list(asins))
        conn.commit()
        return f"Archived {n} products ({', '.join(groups)}). Hidden, not deleted; `restore` brings them back."
    return "Would archive:\n" + "\n".join(out) + "\nCall again with archive=true and groups=[...] to hide them."


def t_restore(conn, a):
    asins = [x.strip().upper() for x in (a.get("asins") or [])]
    if not asins:
        asins = [r[0] for r in conn.execute("SELECT asin FROM products WHERE status = 'dead'")]
    n = db.archive_products(conn, asins, restore=True)
    conn.commit()
    return f"Restored {n} products."


def t_blocked_brands(conn, a):
    from . import restrictions
    cur = restrictions.parse_list(db.get_setting(conn, restrictions.SETTING))
    if a.get("add") or a.get("remove"):
        gone = {x.lower() for x in (a.get("remove") or [])}
        cur = [b for b in cur if b.lower() not in gone]
        cur += [b for b in (a.get("add") or []) if b.lower() not in {x.lower() for x in cur}]
        db.set_setting(conn, restrictions.SETTING, "\n".join(cur))
        conn.commit()
    return "Blocked brands: " + (", ".join(cur) or "none") + ". (Amazon's own brands, gift cards and digital codes are always blocked.)"


def t_outcomes(conn, a):
    o = db.outcomes(conn)
    if not o["sold_lots"]:
        return "Nothing sold yet, so no predicted-vs-actual data. Log purchases and sales to build it."
    lines = [f"{v}: {d['lots']} lots sold, {d['wins']} made money ({d['win_rate']:.0%}), actual {_money(d['profit'])}"
             + (f" vs predicted {_money(d['predicted'])}" if d["predicted_n"] else "") for v, d in o["by_verdict"].items()]
    return f"Total profit {_money(o['total_profit'])} over {o['sold_lots']} lots.\n" + "\n".join(lines)


def t_ungate_targets(conn, a):
    from . import ungate
    t = ungate.targets(conn)
    n = int(a.get("limit") or 8)
    def block(title, groups):
        rows = [f"- {g['name']}: {g['products']} products ({g['strong']} strong), best rank {g['best_rank'] or '-'}, median ${g['median_price'] or '-'}"
                + (f" | e.g. {'; '.join(g['examples'][:2])}" if g["examples"] else "") + (f" | apply: {g['approval_url']}" if g["approval_url"] else "")
                for g in groups[:n]]
        return f"{title}\n" + ("\n".join(rows) or "- none")
    return (block("CATEGORY approvals (one approval opens every product in it):", t["categories"]) + "\n\n"
            + block("BRAND approvals:", t["brands"])
            + f"\n\n{t['closed_to_applications']} products are in brands Amazon is not accepting applications for. 'Strong' = rank under 50,000 and price $10+.")


def t_recheck_gated(conn, a):
    """After the user gets approved for something, drop old gating answers so the next run re-checks them."""
    where, args = ["e.source = 'spapi'"], []
    if a.get("brand"):
        where.append("lower(p.brand) = lower(?)"); args.append(a["brand"])
    if a.get("category"):
        where.append("(lower(p.category) = lower(?) OR lower(e.reason) LIKE ?)"); args += [a["category"], f"%{a['category'].lower()}%"]
    rows = conn.execute(f"SELECT e.asin FROM eligibility e JOIN products p ON p.asin = e.asin WHERE {' AND '.join(where)} AND e.status != 'ungated'", args).fetchall()
    conn.executemany("DELETE FROM eligibility WHERE asin = ?", [(r[0],) for r in rows])
    conn.commit()
    return f"Cleared {len(rows)} old gating answers. Run `run_now` and they will be re-checked against your account."


def _tool(fn, desc, props=None, required=()):
    return {"fn": fn, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)}}


S, N, I = {"type": "string"}, {"type": "number"}, {"type": "integer"}
TOOLS = {
    "status": _tool(t_status, "Overview: counts by verdict, orders imported, last run, what's connected. Call this first."),
    "picks": _tool(t_picks, "Products worth buying. Default BUY + RESEARCH, best first. One line each.",
                   {"limit": I, "verdict": {"type": "string", "enum": VERDICTS}, "min_score": I}),
    "what_changed": _tool(t_what_changed, "Products that got better recently (PASS->RESEARCH/BUY, etc).", {"days": N}),
    "search": _tool(t_search, "Find products by title, brand or ASIN.", {"query": S, "verdict": {"type": "string", "enum": VERDICTS}, "limit": I}, ["query"]),
    "product": _tool(t_product, "Everything known about one product: numbers, stores, channels, history, notes.", {"asin": S}, ["asin"]),
    "todo": _tool(t_todo, "Products you can't judge yet and what's missing for each.", {"limit": I}),
    "add_products": _tool(t_add_products, "Add products from any text containing ASINs or Amazon links.", {"text": S, "origin": S}, ["text"]),
    "import_orders": _tool(t_import_orders, "Import one person's Amazon order history (exporter JSON/CSV text or Amazon's Retail.OrderHistory.csv).",
                           {"content": S, "filename": S, "buyer": S}, ["content"]),
    "log_price": _tool(t_log_price, "Record a store price for a product. pack_qty = store units per Amazon listing.",
                       {"asin": S, "retailer": S, "price": N, "pack_qty": I, "promo": S, "url": S, "in_stock": I, "note": S}, ["asin", "retailer", "price"]),
    "set_sell_price": _tool(t_set_sell_price, "Record what it sells for on eBay or Facebook Marketplace.",
                            {"asin": S, "channel": {"type": "string", "enum": ["ebay", "facebook"]}, "price": N}, ["asin", "channel", "price"]),
    "set_snapshot": _tool(t_set_snapshot, "Record Amazon market data you found yourself (use when Keepa isn't connected).",
                          {"asin": S, "title": S, "brand": S, "category": S, "buy_box": N, "avg_price_90": N, "sales_rank": I,
                           "monthly_sold": I, "offer_count": I, "amazon_price": N, "ebay_sold_price": N, "fba_fee": N}, ["asin"]),
    "set_gated": _tool(t_set_gated, "Record whether the user can sell this on Amazon.",
                       {"asin": S, "status": {"type": "string", "enum": ["ungated", "approval", "blocked"]}, "reason": S}, ["asin", "status"]),
    "set_status": _tool(t_set_status, "pass = not now (still rechecked slowly); dead = hide forever; watch/research/buy.",
                        {"asin": S, "status": {"type": "string", "enum": STATUSES}}, ["asin", "status"]),
    "note": _tool(t_note, "Save a research note on a product so it isn't redone.", {"asin": S, "text": S}, ["asin", "text"]),
    "log_purchase": _tool(t_log_purchase, "Record something the user bought (adds to inventory).",
                          {"asin": S, "qty": I, "unit_cost": N, "store": S, "channel": {"type": "string", "enum": CHANNELS}, "list_price": N}, ["asin", "unit_cost"]),
    "inventory": _tool(t_inventory, "What the user owns and where it's listed."),
    "mark_sold": _tool(t_mark_sold, "Mark an inventory lot sold (id from inventory).", {"id": I, "sold_price": N}, ["id", "sold_price"]),
    "track_seller": _tool(t_track_seller, "Track a seller storefront; their products are added automatically.", {"seller": S, "name": S}, ["seller"]),
    "cleanup": _tool(t_cleanup, "Find junk (Amazon-owned brands, gift cards, cannot-sell, PASS) and optionally archive it. Archived = hidden, not deleted.",
                     {"groups": {"type": "array", "items": {"type": "string", "enum": list(_GROUPS)}}, "archive": {"type": "boolean"}}),
    "restore": _tool(t_restore, "Un-archive products (given ASINs, or everything archived).", {"asins": {"type": "array", "items": S}}),
    "blocked_brands": _tool(t_blocked_brands, "View or edit the user's never-show brand list.",
                            {"add": {"type": "array", "items": S}, "remove": {"type": "array", "items": S}}),
    "outcomes": _tool(t_outcomes, "Predicted vs actual: how each verdict at purchase time performed once sold."),
    "ungate_targets": _tool(t_ungate_targets, "Which category/brand approvals would unlock the most strong products. Use when the user wants to decide what to get ungated.", {"limit": I}),
    "recheck_gated": _tool(t_recheck_gated, "After the user is approved for a brand/category, clear stale gating so it is re-checked (then call run_now).", {"brand": S, "category": S}),
    "run_now": _tool(t_run_now, "Start a refresh + rescore now (data, store prices, gating, alerts)."),
}


# ---------------- JSON-RPC ----------------

def handle(db_path, msg: dict):
    """Process one JSON-RPC message. Returns a response dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if mid is None:  # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                   "serverInfo": {"name": "ghostsignal", "version": __version__}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]} for n, t in TOOLS.items()]})
    if method == "tools/call":
        tool = TOOLS.get(params.get("name"))
        if tool is None:
            return err(-32602, f"Unknown tool {params.get('name')}")
        conn = db.connect(db_path)
        try:
            text, bad = tool["fn"](conn, params.get("arguments") or {}), False
        except KeyError as e:
            text, bad = f"Missing or unknown value: {e}", True
        except (ValueError, TypeError) as e:
            text, bad = f"Bad input: {e}", True
        except Exception as e:  # keep the agent informed instead of dropping the connection
            text, bad = f"Error: {e}", True
        finally:
            conn.close()
        return ok({"content": [{"type": "text", "text": text}], "isError": bad})
    return err(-32601, f"Method not found: {method}")
