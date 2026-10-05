"""`gs` — GhostSignal command line.

  gs demo                                  seed sample data so the dashboard has something to show
  gs import-orders FILE --buyer me         Amazon order history (scraper JSON/CSV or privacy export)
  gs import-products FILE --source NAME    any CSV with ASINs (Stealth Seller, Keepa, sheets)
  gs add B0XXXXXXX ...  |  gs add -        add ASINs (or paste text/URLs on stdin)
  gs source add ASIN walmart 8.50 [--pack 2 --promo B2G1 --url ... --oos]
  gs snap ASIN --buy-box 21.80 --rank 21200 --monthly 100 --offers 14
  gs refresh [--stale-days 3]              pull Keepa data (needs KEEPA_API_KEY)
  gs seller add SELLER_ID | gs seller pull shadow storefronts via Keepa
  gs enrich                                AI risk flags (needs ANTHROPIC_API_KEY)
  gs score [--alert]                       score everything, alert on changes
  gs run                                   refresh + score + alert (put this on cron)
  gs top [-n 20] [--verdict BUY]           ranked opportunities
  gs show ASIN | gs links ASIN             one product in detail / where to source it
  gs status ASIN pass                      watch | buy | research | pass | dead
  gs export asins [--verdict RESEARCH]     ASIN list to paste into Stealth Seller / Keepa
  gs export csv [-o out.csv]               full table
  gs serve [--port 8787]                   dashboard
  gs setup                                 what's connected and what's left to do
  gs gated ASIN ungated|approval|blocked   record whether you can sell it
  gs check-gated [--all]                   real gated check via Amazon SP-API
  gs fees                                  real Amazon fees via SP-API
  gs location "Detroit, Michigan, United States"   your area for store prices
  gs prices [ASIN ...] [--max 25]          store prices near you (needs SERPAPI_KEY)
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone

from . import db, engine, importers
from .sources import marketplace_links, retailer_links


def _conn(args):
    return db.connect(args.db)


def _ranked(conn, verdict=None, limit=None):
    out = []
    for (asin,) in conn.execute("SELECT asin FROM products WHERE status NOT IN ('dead')"):
        v = engine.product_view(conn, asin)
        if verdict and v["verdict"] != verdict.upper():
            continue
        out.append(v)
    out.sort(key=lambda v: (-{"BUY": 2, "RESEARCH": 1, "PASS": 0}[v["verdict"]], -v["score"]))
    return out[:limit] if limit else out


def _money(x):
    if not isinstance(x, (int, float)):
        return "—"
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def cmd_import_orders(a):
    conn = _conn(a)
    for f in a.files:
        r = importers.import_orders(conn, f, a.buyer)
        print(f"{f}: {r['added']} order lines added, {r['skipped']} skipped (of {r['rows']})")


def cmd_import_products(a):
    conn = _conn(a)
    for f in a.files:
        r = importers.import_products(conn, f, a.source)
        print(f"{f}: {r['products']} products, {r['snapshots']} snapshots, {r['sources']} sources")


def cmd_add(a):
    conn = _conn(a)
    text = sys.stdin.read() if a.asins == ["-"] else " ".join(a.asins)
    print(f"{importers.import_asin_text(conn, text, a.origin)} ASINs added/known")


def cmd_source(a):
    conn = _conn(a)
    db.upsert_product(conn, a.asin)
    db.add_source(conn, a.asin, a.retailer, a.price, pack_qty=a.pack, promo=a.promo, url=a.url,
                  in_stock=0 if a.oos else (1 if a.in_stock else None), note=a.note)
    conn.commit()
    print(f"Logged {a.retailer} @ {_money(a.price)} for {a.asin.upper()}")


def cmd_snap(a):
    conn = _conn(a)
    db.upsert_product(conn, a.asin, title=a.title, category=a.category)
    db.add_snapshot(conn, a.asin, "manual", buy_box=a.buy_box, avg_price_90=a.avg, sales_rank=a.rank,
                    monthly_sold=a.monthly, offer_count=a.offers, amazon_price=a.amazon,
                    ebay_sold_price=a.ebay, fba_fee=a.fba_fee)
    conn.commit()
    print(f"Snapshot saved for {a.asin.upper()}")


def _stale_asins(conn, days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r[0] for r in conn.execute(
        "SELECT asin FROM products WHERE status NOT IN ('dead','pass') AND (last_checked IS NULL OR last_checked < ?)",
        (cutoff,))]


def cmd_refresh(a):
    from . import keepa
    conn = _conn(a)
    asins = _stale_asins(conn, a.stale_days)
    if not asins:
        print("Nothing stale.")
        return
    print(f"Refreshing {len(asins)} products from Keepa…")
    print(keepa.refresh(conn, asins))


def cmd_seller(a):
    import os
    from . import keepa
    conn = _conn(a)
    if a.action == "add":
        sid = importers.parse_seller_id(a.seller_id)
        if not sid:
            sys.exit("Couldn't find a seller ID. Paste the storefront URL (has seller=A…) or the ID itself.")
        db.add_seller(conn, sid)
        conn.commit()
        if os.environ.get("KEEPA_API_KEY"):
            print(f"Tracking {sid}: {len(keepa.seller_storefront(conn, sid))} storefront ASINs")
        else:
            print(f"Tracking {sid}. Add KEEPA_API_KEY to pull their products.")
    elif a.action == "pull":
        for (sid,) in conn.execute("SELECT seller_id FROM tracked_sellers WHERE status = 'active'").fetchall():
            print(f"{sid}: {len(keepa.seller_storefront(conn, sid))} ASINs")
    else:
        for r in conn.execute("SELECT * FROM tracked_sellers"):
            print(dict(r))


def cmd_enrich(a):
    from . import enrich
    print(f"Enriched {enrich.enrich(_conn(a), force=a.force)} products")


def cmd_score(a):
    changes = engine.run(_conn(a))
    print(f"Scored. {len(changes)} change(s) worth a look.")
    if a.alert:
        engine.send_alerts(changes)
    else:
        for c in changes:
            print(engine.format_alert(c), end="\n\n")


def run_pipeline(conn, stale_days: float = 3, max_prices: int = 50):
    """Everything `gs run` does. Each step only runs if its key is connected."""
    import os
    if os.environ.get("KEEPA_API_KEY"):
        from . import keepa
        for (sid,) in conn.execute("SELECT seller_id FROM tracked_sellers WHERE status = 'active'").fetchall():
            keepa.seller_storefront(conn, sid)
        stale = _stale_asins(conn, stale_days)
        if stale:
            print("keepa:", keepa.refresh(conn, stale), flush=True)
    from . import spapi, stores
    if spapi.configured():
        unchecked = _unchecked_gated(conn)
        if unchecked:
            print("gated check:", spapi.check_gated(conn, unchecked), flush=True)
        print("fees updated:", spapi.update_fees(conn, _live_asins(conn)), flush=True)
    if stores.configured():
        print("store prices:", stores.find_prices(conn, stores.due_for_check(conn, max_prices)), flush=True)
    if os.environ.get("ANTHROPIC_API_KEY"):
        from . import enrich
        print("enriched:", enrich.enrich(conn), flush=True)
    changes = engine.run(conn)
    print(f"{len(changes)} alert(s)", flush=True)
    engine.send_alerts(changes)
    return changes


def cmd_run(a):
    run_pipeline(_conn(a), a.stale_days, a.max_prices)


def _live_asins(conn):
    return [r[0] for r in conn.execute("SELECT asin FROM products WHERE status NOT IN ('dead','pass')")]


def _unchecked_gated(conn, days=30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r[0] for r in conn.execute(
        """SELECT p.asin FROM products p LEFT JOIN eligibility e ON e.asin = p.asin
           WHERE p.status NOT IN ('dead','pass') AND (e.asin IS NULL OR (e.source = 'spapi' AND e.checked_at < ?))""",
        (cutoff,))]


def cmd_gated(a):
    conn = _conn(a)
    db.set_eligibility(conn, a.asin, a.status, "manual")
    conn.commit()
    print(f"{a.asin.upper()} → {a.status}")


def cmd_check_gated(a):
    from . import spapi
    conn = _conn(a)
    asins = _live_asins(conn) if a.all else _unchecked_gated(conn)
    print(f"Checking {len(asins)} products against your seller account…")
    print(spapi.check_gated(conn, asins))


def cmd_fees(a):
    from . import spapi
    conn = _conn(a)
    print(f"Updated fees for {spapi.update_fees(conn, _live_asins(conn))} products")


def cmd_setup(a):
    from .setup_status import checklist
    print("GhostSignal setup\n")
    for item in checklist(_conn(a)):
        cost = f" ({item['cost']})" if item["cost"] else ""
        print(f"{'✅' if item['done'] else '⬜'} {item['label']}{cost}")
        if not item["done"]:
            print(f"     {item['what']}\n     → {item['how']}")
    print("\nNothing above is required to start: gs serve works with manual data.")


def cmd_location(a):
    conn = _conn(a)
    db.set_setting(conn, "location", a.location)
    conn.commit()
    print(f"Area set to {a.location}")


def cmd_prices(a):
    from . import stores
    conn = _conn(a)
    asins = [x.upper() for x in a.asins] or stores.due_for_check(conn, a.max)
    print(f"Looking up store prices for {len(asins)} products…")
    print(stores.find_prices(conn, asins))


def cmd_top(a):
    rows = _ranked(_conn(a), a.verdict, a.n)
    print(f"{'VERDICT':9} {'SCR':>3}  {'ASIN':10}  {'SELL':>8} {'COST':>8} {'PROFIT':>7} {'ROI':>5}  TITLE")
    for v in rows:
        e = v["economics"]
        roi = f"{e['roi']:.0%}" if e["roi"] is not None else "—"
        print(f"{v['verdict']:9} {v['score']:>3}  {v['asin']:10}  {_money(e['sale_price']):>8} "
              f"{_money(e['cost']):>8} {_money(e['profit']):>7} {roi:>5}  {(v['title'] or '')[:60]}")


def cmd_show(a):
    v = engine.product_view(_conn(a), a.asin.upper())
    e = v["economics"]
    print(f"{v['title']}\n{v['asin']} · {v['brand'] or ''} · {v['category'] or ''}")
    print(f"\n{v['verdict']}  {v['score']}/100")
    print(f"Sell {_money(e['sale_price'])}  Fees {_money((e['referral_fee'] or 0) + (e['fba_fee'] or 0))}  "
          f"Max cost {_money(e['max_cost'])}  Cost {_money(e['cost'])}  Profit {_money(e['profit'])}")
    for r in v["reasons"]:
        print(f"  + {r}")
    for f in v["flags"]:
        print(f"  ! {f}")
    if v["sources"]:
        print("\nSources:")
        for s in v["sources"]:
            print(f"  {s['retailer']:12} {_money(s['price'])} x{s['pack_qty']} {s['promo'] or ''} {s['url'] or ''}")
    if v["links"].get("keepa"):
        print(f"\nKeepa: {v['links']['keepa']}")


def cmd_links(a):
    conn = _conn(a)
    p = conn.execute("SELECT * FROM products WHERE asin = ?", (a.asin.upper(),)).fetchone()
    if p is None:
        sys.exit(f"Unknown ASIN {a.asin}")
    for k, url in marketplace_links(p["asin"], p["title"]).items():
        print(f"{k:14} {url}")
    for k, r in retailer_links(p["title"], p["upc"]).items():
        print(f"{r['name']:14} {r['url']}")


def cmd_status(a):
    conn = _conn(a)
    conn.execute("UPDATE products SET status = ? WHERE asin = ?", (a.status, a.asin.upper()))
    conn.commit()
    print(f"{a.asin.upper()} → {a.status}")


def cmd_export(a):
    conn = _conn(a)
    out = open(a.output, "w", newline="") if a.output else sys.stdout
    if a.what == "asins":
        rows = _ranked(conn, a.verdict) if a.verdict else [
            {"asin": r[0]} for r in conn.execute("SELECT asin FROM products WHERE status NOT IN ('dead','pass')")]
        out.write("\n".join(r["asin"] for r in rows) + "\n")
    else:
        w = csv.writer(out)
        w.writerow(["asin", "title", "brand", "category", "verdict", "score", "sale_price", "max_cost", "cost",
                    "profit", "roi", "best_source", "monthly_sold", "sales_rank", "offers", "network_orders", "keepa"])
        for v in _ranked(conn, a.verdict):
            e, s = v["economics"], v["snapshot"] or {}
            w.writerow([v["asin"], v["title"], v["brand"], v["category"], v["verdict"], v["score"], e["sale_price"],
                        e["max_cost"], e["cost"], e["profit"], e["roi"],
                        (v["best_source"] or {}).get("retailer"), s.get("monthly_sold"), s.get("sales_rank"),
                        s.get("offer_count"), v["orders"]["orders"], v["links"].get("keepa", "")])
    if a.output:
        out.close()
        print(f"Wrote {a.output}")


def cmd_serve(a):
    from .server import serve
    serve(a.db, a.host, a.port)


def cmd_clear_demo(a):
    from .demo import clear_demo
    print(f"Removed {clear_demo(_conn(a))} sample products. Your own data is untouched.")


def cmd_demo(a):
    from .demo import seed
    conn = _conn(a)
    seed(conn)
    engine.run(conn)
    print("Demo data loaded. Try: gs top   or   gs serve")


def load_env(path=".env"):
    """Read KEY=value lines from .env (project folder) into the environment."""
    import os
    from pathlib import Path
    for f in (Path(path), Path(__file__).resolve().parent.parent / ".env"):
        if f.is_file():
            for line in f.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if v:
                        os.environ.setdefault(k.strip(), v)
            break


def main(argv=None):
    load_env()
    p = argparse.ArgumentParser(prog="gs", description="GhostSignal — find the signal, make the move.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--db", default=None, help="SQLite path (default data/ghostsignal.db or $GHOSTSIGNAL_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import-orders"); s.add_argument("files", nargs="+"); s.add_argument("--buyer", required=True)
    s.set_defaults(fn=cmd_import_orders)
    s = sub.add_parser("import-products"); s.add_argument("files", nargs="+"); s.add_argument("--source", default="csv")
    s.set_defaults(fn=cmd_import_products)
    s = sub.add_parser("add"); s.add_argument("asins", nargs="+"); s.add_argument("--origin", default="manual")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("source"); s.add_argument("action", choices=["add"]); s.add_argument("asin")
    s.add_argument("retailer"); s.add_argument("price", type=float)
    s.add_argument("--pack", type=int, default=1, help="retail units per Amazon unit")
    s.add_argument("--promo", default=""); s.add_argument("--url", default=""); s.add_argument("--note", default="")
    s.add_argument("--oos", action="store_true"); s.add_argument("--in-stock", action="store_true")
    s.set_defaults(fn=cmd_source)

    s = sub.add_parser("snap"); s.add_argument("asin")
    for flag in ("--buy-box", "--avg", "--amazon", "--ebay", "--fba-fee"):
        s.add_argument(flag, type=float)
    for flag in ("--rank", "--monthly", "--offers"):
        s.add_argument(flag, type=int)
    s.add_argument("--title"); s.add_argument("--category")
    s.set_defaults(fn=cmd_snap)

    s = sub.add_parser("refresh"); s.add_argument("--stale-days", type=float, default=3); s.set_defaults(fn=cmd_refresh)
    s = sub.add_parser("seller"); s.add_argument("action", choices=["add", "pull", "list"])
    s.add_argument("seller_id", nargs="?"); s.set_defaults(fn=cmd_seller)
    s = sub.add_parser("enrich"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_enrich)
    s = sub.add_parser("score"); s.add_argument("--alert", action="store_true"); s.set_defaults(fn=cmd_score)
    s = sub.add_parser("run"); s.add_argument("--stale-days", type=float, default=3)
    s.add_argument("--max-prices", type=int, default=50, help="cap store-price lookups per run (SerpAPI cost)")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("prices"); s.add_argument("asins", nargs="*"); s.add_argument("--max", type=int, default=25)
    s.set_defaults(fn=cmd_prices)
    s = sub.add_parser("location"); s.add_argument("location", help='e.g. "Detroit, Michigan, United States"')
    s.set_defaults(fn=cmd_location)
    s = sub.add_parser("top"); s.add_argument("-n", type=int, default=25); s.add_argument("--verdict")
    s.set_defaults(fn=cmd_top)
    s = sub.add_parser("show"); s.add_argument("asin"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("links"); s.add_argument("asin"); s.set_defaults(fn=cmd_links)
    s = sub.add_parser("status"); s.add_argument("asin")
    s.add_argument("status", choices=["watch", "buy", "research", "pass", "dead"]); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("export"); s.add_argument("what", choices=["asins", "csv"]); s.add_argument("--verdict")
    s.add_argument("-o", "--output"); s.set_defaults(fn=cmd_export)
    s = sub.add_parser("serve"); s.add_argument("--host", default=None); s.add_argument("--port", type=int, default=None)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("demo"); s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("clear-demo"); s.set_defaults(fn=cmd_clear_demo)
    s = sub.add_parser("setup"); s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("gated"); s.add_argument("asin"); s.add_argument("status", choices=list(db.GATED_STATUSES))
    s.set_defaults(fn=cmd_gated)
    s = sub.add_parser("check-gated"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_check_gated)
    s = sub.add_parser("fees"); s.set_defaults(fn=cmd_fees)

    a = p.parse_args(argv)
    if a.cmd == "seller" and a.action == "add" and not a.seller_id:
        p.error("seller add needs a SELLER_ID")
    a.fn(a)


if __name__ == "__main__":
    main()
