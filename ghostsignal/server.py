"""Dashboard server — stdlib only. `gs serve` then open http://127.0.0.1:8787"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, engine, importers

WEB = Path(__file__).parent / "web"
ASIN_PATH = re.compile(r"^/api/products/([A-Z0-9]{10})(?:/(source|status|snapshot|gated|prices|channel|inventory))?$")
ORDER = {"BUY": 2, "RESEARCH": 1, "PASS": 0}
INV_PATH = re.compile(r"^/api/inventory/(\d+)$")
SELLER_PATH = re.compile(r"^/api/sellers/([A-Z0-9]{10,20})/(status|remove)$")


def recent_changes(conn, limit=12):
    """Products whose latest verdict is better than the one before it."""
    out = []
    rows = conn.execute(
        """SELECT s.asin, s.verdict, s.score, s.created_at, p.title FROM signals s JOIN products p ON p.asin = s.asin
           WHERE p.status != 'dead' ORDER BY s.asin, s.id DESC""").fetchall()
    last = {}
    for r in rows:
        last.setdefault(r["asin"], []).append(r)
    for asin, sigs in last.items():
        if len(sigs) >= 2 and ORDER[sigs[0]["verdict"]] > ORDER[sigs[1]["verdict"]]:
            out.append({"asin": asin, "title": sigs[0]["title"], "from": sigs[1]["verdict"],
                        "to": sigs[0]["verdict"], "score": sigs[0]["score"], "at": sigs[0]["created_at"]})
    return sorted(out, key=lambda c: c["at"], reverse=True)[:limit]


def sellers(conn):
    out = []
    for r in conn.execute("SELECT * FROM tracked_sellers ORDER BY added_at DESC"):
        d = dict(r)
        d["products"] = conn.execute("SELECT COUNT(*) FROM products WHERE origin LIKE ?",
                                     (f"%seller:{r['seller_id']}%",)).fetchone()[0]
        d["buys"] = sum(1 for (a,) in conn.execute("SELECT asin FROM products WHERE origin LIKE ?",
                                                   (f"%seller:{r['seller_id']}%",))
                        if (db.last_signal(conn, a) or {"verdict": ""})["verdict"] == "BUY")
        out.append(d)
    return out


_run_lock = threading.Lock()


def refresh_in_background(db_path):
    """After new data lands, pull Keepa/store prices/etc. for whatever keys are connected."""
    def work():
        if not _run_lock.acquire(blocking=False):
            return
        try:
            from .cli import run_pipeline
            c = db.connect(db_path)
            run_pipeline(c)
            c.close()
        except Exception as e:
            print("background refresh failed:", e, flush=True)
        finally:
            _run_lock.release()
    threading.Thread(target=work, daemon=True).start()


def make_handler(db_path):
    password = os.environ.get("GHOSTSIGNAL_PASSWORD", "")
    root = Path(__file__).resolve().parent.parent

    class Handler(BaseHTTPRequestHandler):
        def _authed(self) -> bool:
            """HTTP Basic auth. Any username, password = GHOSTSIGNAL_PASSWORD. /health is open."""
            if not password or self.path == "/health":
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    given = base64.b64decode(header[6:]).decode().partition(":")[2]
                    if hmac.compare_digest(given.encode(), password.encode()):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="GhostSignal"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def log_message(self, fmt, *args):  # quieter
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._authed():
                return None
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, {"ok": True})
            if url.path == "/tools/amazon_orders_scraper.js":
                return self._send(200, (root / "tools" / "amazon_orders_scraper.js").read_bytes(),
                                  "text/javascript; charset=utf-8")
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            conn = db.connect(db_path)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/products":
                    rows = [engine.product_view(conn, r[0]) for r in conn.execute(
                        "SELECT asin FROM products WHERE status != 'dead'")]
                    if qs.get("verdict"):
                        rows = [r for r in rows if r["verdict"] == qs["verdict"].upper()]
                    if qs.get("q"):
                        q = qs["q"].lower()
                        rows = [r for r in rows if q in f"{r['asin']} {r['title']} {r['brand']}".lower()]
                    rows.sort(key=lambda r: (-ORDER[r["verdict"]], -r["score"]))
                    return self._send(200, rows)
                m = ASIN_PATH.match(url.path)
                if m and not m.group(2):
                    return self._send(200, engine.product_view(conn, m.group(1)))
                if url.path == "/api/stats":
                    c = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
                    return self._send(200, {
                        "products": c("SELECT COUNT(*) FROM products WHERE status != 'dead'"),
                        "orders": c("SELECT COUNT(*) FROM orders"),
                        "buyers": c("SELECT COUNT(DISTINCT buyer) FROM orders"),
                        "sellers": c("SELECT COUNT(*) FROM tracked_sellers"),
                        "last_run": c("SELECT MAX(created_at) FROM signals"),
                        "inventory_units": c("SELECT COALESCE(SUM(qty - sold_qty), 0) FROM inventory WHERE status != 'sold'"),
                        "inventory_cost": c("SELECT COALESCE(SUM((qty - sold_qty) * unit_cost), 0) FROM inventory WHERE status != 'sold'"),
                        "realized_profit": c("SELECT COALESCE(SUM((sold_price - COALESCE(unit_cost, 0)) * sold_qty), 0) FROM inventory WHERE status = 'sold'"),
                    })
                if url.path == "/api/demo":
                    from .demo import has_demo
                    return self._send(200, {"has_demo": has_demo(conn)})
                if url.path == "/api/setup":
                    from .setup_status import checklist
                    return self._send(200, {"items": checklist(conn), "location": db.get_setting(conn, "location")})
                if url.path == "/api/sellers":
                    return self._send(200, sellers(conn))
                if url.path == "/api/inventory":
                    return self._send(200, db.inventory_rows(conn))
                if url.path == "/api/changes":
                    return self._send(200, recent_changes(conn))
                if url.path == "/api/export.csv":
                    import csv
                    import io
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    w.writerow(["asin", "title", "brand", "signal", "score", "can_sell", "sell_price", "cost", "max_cost",
                                "profit", "roi", "sales_per_month", "rank", "offers", "network_orders", "best_store"])
                    for (asin,) in conn.execute("SELECT asin FROM products WHERE status != 'dead'"):
                        v = engine.product_view(conn, asin)
                        e, sn = v["economics"], v["snapshot"] or {}
                        w.writerow([asin, v["title"], v["brand"], v["verdict"], v["score"], v["gated"], e["sale_price"],
                                    e["cost"], e["max_cost"], e["profit"], e["roi"], sn.get("monthly_sold"),
                                    sn.get("sales_rank"), sn.get("offer_count"), v["orders"]["orders"],
                                    (v["best_source"] or {}).get("retailer")])
                    self.send_response(200)
                    data = buf.getvalue().encode()
                    self.send_header("Content-Type", "text/csv")
                    self.send_header("Content-Disposition", 'attachment; filename="ghostsignal-products.csv"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                if url.path == "/api/asins":
                    rows = conn.execute("SELECT asin FROM products WHERE status NOT IN ('dead','pass')").fetchall()
                    return self._send(200, "\n".join(r[0] for r in rows).encode(), "text/plain")
                return self._send(404, {"error": "not found"})
            except KeyError:
                return self._send(404, {"error": "unknown asin"})
            finally:
                conn.close()

        def do_POST(self):
            if not self._authed():
                return None
            url = urlparse(self.path)
            conn = db.connect(db_path)
            try:
                body = self._body()
                m = ASIN_PATH.match(url.path)
                if m and m.group(2) == "source":
                    db.add_source(conn, m.group(1), body["retailer"], float(body["price"]),
                                  pack_qty=body.get("pack_qty") or 1, promo=body.get("promo"),
                                  url=body.get("url"), in_stock=body.get("in_stock"))
                    conn.commit()
                    return self._send(200, engine.product_view(conn, m.group(1)))
                if m and m.group(2) == "status":
                    conn.execute("UPDATE products SET status = ? WHERE asin = ?", (body["status"], m.group(1)))
                    conn.commit()
                    return self._send(200, {"ok": True})
                if m and m.group(2) == "channel":
                    db.set_channel_price(conn, m.group(1), body["channel"], float(body["price"]))
                    conn.commit()
                    return self._send(200, engine.product_view(conn, m.group(1)))
                if m and m.group(2) == "inventory":
                    db.add_inventory(conn, m.group(1), int(body.get("qty") or 1),
                                     float(body["unit_cost"]) if body.get("unit_cost") not in (None, "") else None,
                                     body.get("store") or "", body.get("channel") or "",
                                     float(body["list_price"]) if body.get("list_price") not in (None, "") else None)
                    conn.commit()
                    return self._send(200, engine.product_view(conn, m.group(1)))
                im = INV_PATH.match(url.path)
                if im:
                    if body.get("delete"):
                        conn.execute("DELETE FROM inventory WHERE id = ?", (int(im.group(1)),))
                    else:
                        db.update_inventory(conn, int(im.group(1)), **body)
                    conn.commit()
                    return self._send(200, {"ok": True})
                if url.path == "/api/items":
                    if not (body.get("title") or "").strip():
                        return self._send(400, {"error": "Give the item a name"})
                    item_id = db.new_item_id(conn)
                    db.upsert_product(conn, item_id, title=body["title"].strip(), brand=body.get("brand"),
                                      category=body.get("category"), origin="manual")
                    for ch in ("ebay", "facebook"):
                        if body.get(ch):
                            db.set_channel_price(conn, item_id, ch, float(body[ch]))
                    conn.commit()
                    return self._send(200, {"asin": item_id})
                if m and m.group(2) == "prices":
                    from . import stores
                    if not stores.configured():
                        return self._send(400, {"error": "Store prices need SERPAPI_KEY in .env (see Setup)"})
                    stores.find_prices(conn, [m.group(1)])
                    return self._send(200, engine.product_view(conn, m.group(1)))
                if url.path == "/api/sellers":
                    sid = importers.parse_seller_id(body.get("seller", ""))
                    if not sid:
                        return self._send(400, {"error": "Paste a storefront URL (contains seller=A…) or a seller ID"})
                    db.add_seller(conn, sid, body.get("name") or None)
                    conn.commit()
                    msg = "Tracking. Add a Keepa key to pull their products."
                    import os
                    if os.environ.get("KEEPA_API_KEY"):
                        from . import keepa
                        msg = f"Tracking. Pulled {len(keepa.seller_storefront(conn, sid))} products."
                    return self._send(200, {"seller_id": sid, "message": msg})
                if url.path == "/api/sellers/refresh":
                    import os
                    if not os.environ.get("KEEPA_API_KEY"):
                        return self._send(400, {"error": "Pulling seller products needs KEEPA_API_KEY (see Setup)"})
                    from . import keepa
                    n = 0
                    for (sid,) in conn.execute("SELECT seller_id FROM tracked_sellers WHERE status = 'active'").fetchall():
                        n += len(keepa.seller_storefront(conn, sid))
                    engine.run(conn)
                    return self._send(200, {"message": f"Pulled {n} products"})
                sm = SELLER_PATH.match(url.path)
                if sm and sm.group(2) == "status":
                    conn.execute("UPDATE tracked_sellers SET status = ? WHERE seller_id = ?", (body["status"], sm.group(1)))
                    conn.commit()
                    return self._send(200, {"ok": True})
                if sm and sm.group(2) == "remove":
                    conn.execute("DELETE FROM tracked_sellers WHERE seller_id = ?", (sm.group(1),))
                    conn.commit()
                    return self._send(200, {"ok": True})
                if url.path == "/api/settings":
                    if "location" in body:
                        db.set_setting(conn, "location", body["location"].strip())
                    conn.commit()
                    return self._send(200, {"ok": True})
                if m and m.group(2) == "gated":
                    db.set_eligibility(conn, m.group(1), body["status"], "manual")
                    conn.commit()
                    return self._send(200, engine.product_view(conn, m.group(1)))
                if m and m.group(2) == "snapshot":
                    db.add_snapshot(conn, m.group(1), "manual", **{k: v for k, v in body.items() if v not in ("", None)})
                    conn.commit()
                    return self._send(200, engine.product_view(conn, m.group(1)))
                if url.path == "/api/add":
                    return self._send(200, {"added": importers.import_asin_text(conn, body.get("text", ""))})
                if url.path in ("/api/import/orders", "/api/import/products"):
                    if url.path.endswith("orders") and not (body.get("buyer") or "").strip():
                        mm = re.match(r"amazon-orders-(.+?)(?:\s\(\d+\))?\.(?:json|csv)$", body.get("filename") or "", re.I)
                        body["buyer"] = mm.group(1) if mm else "me"
                    suffix = Path(body.get("filename") or "upload.csv").suffix or ".csv"
                    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as f:
                        f.write(body["content"])
                    try:
                        if url.path.endswith("orders"):
                            r = importers.import_orders(conn, f.name, body["buyer"].strip())
                            refresh_in_background(db_path)
                        else:
                            r = importers.import_products(conn, f.name, body.get("source") or "upload")
                    finally:
                        Path(f.name).unlink(missing_ok=True)
                    return self._send(200, r)
                if url.path == "/api/demo/clear":
                    from .demo import clear_demo
                    return self._send(200, {"removed": clear_demo(conn)})
                if url.path == "/api/score":
                    changes = engine.run(conn)
                    return self._send(200, {"changes": [
                        {"asin": c["asin"], "title": c["title"], "kind": c["kind"],
                         "score": c["signal"].score, "verdict": c["signal"].verdict} for c in changes]})
                return self._send(404, {"error": "not found"})
            except (KeyError, ValueError, json.JSONDecodeError, RuntimeError, OSError) as e:
                return self._send(400, {"error": str(e)})
            finally:
                conn.close()

    return Handler


def _scheduler(db_path, hours: float):
    """Refresh + score + alert on a timer, inside the same service (no separate cron needed)."""
    from .cli import run_pipeline
    while True:
        time.sleep(hours * 3600)
        try:
            conn = db.connect(db_path)
            run_pipeline(conn)
            conn.close()
        except Exception as e:  # keep the loop alive
            print("scheduled run failed:", e, flush=True)


def serve(db_path=None, host=None, port=None):
    on_railway = bool(os.environ.get("PORT"))
    host = host or ("0.0.0.0" if on_railway else "127.0.0.1")
    port = int(port or os.environ.get("PORT") or 8787)
    if host != "127.0.0.1" and not os.environ.get("GHOSTSIGNAL_PASSWORD"):
        raise SystemExit("Refusing to start on a public address without a password.\n"
                         "Set GHOSTSIGNAL_PASSWORD (it protects your order data), then start again.")
    db.connect(db_path).close()
    hours = float(os.environ.get("GHOSTSIGNAL_AUTO_RUN_HOURS") or 0)
    if hours:
        threading.Thread(target=_scheduler, args=(db_path, hours), daemon=True).start()
        print(f"Auto-run every {hours:g}h", flush=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"GhostSignal dashboard on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
