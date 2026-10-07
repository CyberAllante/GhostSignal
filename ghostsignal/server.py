"""Dashboard server — stdlib only. `gs serve` then open http://127.0.0.1:8787"""

from __future__ import annotations

import base64
import gzip
import hmac
import io
import zipfile
import json
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, engine, importers, invites, mcp, restrictions, ungate

WEB = Path(__file__).parent / "web"
ASIN_PATH = re.compile(r"^/api/products/([A-Z0-9]{10})(?:/(source|status|snapshot|gated|prices|channel|inventory))?$")
ORDER = {"BUY": 2, "RESEARCH": 1, "PASS": 0}
INV_PATH = re.compile(r"^/api/inventory/(\d+)$")
SELLER_PATH = re.compile(r"^/api/sellers/([A-Z0-9]{10,20})/(status|remove)$")


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


def refresh_in_background(db_path, stale_days=None):
    """After new data lands, pull Keepa/store prices/etc. for whatever keys are connected."""
    def work():
        if not _run_lock.acquire(blocking=False):
            return
        try:
            from .cli import run_pipeline
            c = db.connect(db_path)
            run_pipeline(c, stale_days)
            c.close()
            _invalidate_lists()
        except Exception as e:
            print("background refresh failed:", e, flush=True)
        finally:
            _run_lock.release()
    threading.Thread(target=work, daemon=True).start()


def discover_in_background(db_path, **kw):
    """Search the catalog for new products, then run the normal pipeline on them (gating, market data, scores)."""
    def work():
        if not _run_lock.acquire(blocking=False):
            return
        try:
            from . import discover
            from .cli import run_pipeline
            c = db.connect(db_path)
            print("discover:", discover.run(c, **kw), flush=True)
            run_pipeline(c)
            c.close()
            _invalidate_lists()
        except Exception as e:
            print("discover failed:", e, flush=True)
        finally:
            _run_lock.release()
    threading.Thread(target=work, daemon=True).start()


def is_running() -> bool:
    return _run_lock.locked()


_pin_fails = {"n": 0, "locked_until": 0.0}
PIN_MAX_FAILS, PIN_LOCK_SECONDS = 5, 3600


_list_cache = {"key": None, "at": 0.0, "rows": None}
_ungate_cache = {"at": 0.0, "data": None}
LIST_TTL = 60


def _invalidate_lists():
    _list_cache["at"] = 0.0
    _ungate_cache["at"] = 0.0


def make_handler(db_path):
    password = os.environ.get("GHOSTSIGNAL_PASSWORD", "")
    root = Path(__file__).resolve().parent.parent

    class Handler(BaseHTTPRequestHandler):
        def _session(self) -> str:
            return hmac.new(password.encode(), b"gs-session-v1", "sha256").hexdigest()

        def _authed(self) -> bool:
            """Session cookie (login page), or Basic/Bearer for scripts + MCP. /health, /login, PWA assets are open."""
            if not password or self.path == "/health":
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), password.encode()):
                return True
            if header.startswith("Basic "):
                try:
                    given = base64.b64decode(header[6:]).decode().partition(":")[2]
                    if hmac.compare_digest(given.encode(), password.encode()):
                        return True
                except Exception:
                    pass
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "gs_session" and hmac.compare_digest(v.encode(), self._session().encode()):
                    return True
            if self.command == "GET" and "text/html" in self.headers.get("Accept", ""):
                self.send_response(302)
                self.send_header("Location", "/login")
            else:
                self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _public_asset(self, path) -> bool:
            assets = {
                "/login": ("login.html", "text/html; charset=utf-8"),
                "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                "/icon.svg": ("icon.svg", "image/svg+xml"),
            }
            if path not in assets:
                return False
            name, ctype = assets[path]
            self._send(200, (WEB / name).read_bytes(), ctype)
            return True

        def _login(self):
            """Accepts the long password, or a short PIN (GHOSTSIGNAL_PIN). PIN guesses are limited:
            5 wrong tries disable the PIN for an hour (the long password keeps working)."""
            n = int(self.headers.get("Content-Length") or 0)
            given = parse_qs(self.rfile.read(n).decode()).get("password", [""])[0].strip()
            pin = os.environ.get("GHOSTSIGNAL_PIN", "")
            ok = bool(password) and hmac.compare_digest(given.encode(), password.encode())
            if not ok and pin and time.time() >= _pin_fails["locked_until"]:
                if hmac.compare_digest(given.encode(), pin.encode()):
                    ok = True
                    _pin_fails["n"] = 0
                elif given.isdigit():     # only count PIN-shaped guesses against the lock
                    _pin_fails["n"] += 1
                    if _pin_fails["n"] >= PIN_MAX_FAILS:
                        _pin_fails.update(n=0, locked_until=time.time() + PIN_LOCK_SECONDS)
            self.send_response(302)
            if ok:
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"gs_session={self._session()}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Lax")
            else:
                locked = bool(pin) and time.time() < _pin_fails["locked_until"]
                self.send_header("Location", "/login?error=" + ("locked" if locked else "1"))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):  # quieter
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            zipped = len(data) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or "")
            if zipped:
                data = gzip.compress(data, compresslevel=5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if zipped:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _public_url(self) -> str:
            base = os.environ.get("GHOSTSIGNAL_PUBLIC_URL")
            if base:
                return base.rstrip("/")
            proto = self.headers.get("X-Forwarded-Proto") or "http"
            return f"{proto}://{self.headers.get('Host', 'localhost')}"

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/logout":
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Set-Cookie", "gs_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if self._public_asset(path):
                return None
            if path in ("/contribute", "/api/invite/check", "/extension.zip") or (
                    path == "/tools/amazon_orders_scraper.js" and "c=" in self.path):
                return self._invite_get(path)
            if self.path.startswith("/mcp"):
                if not self._authed():
                    return None
                self.send_response(405)  # no server-initiated stream; POST only
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if not self._authed():
                return None
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, {"ok": True})
            if url.path == "/tools/amazon_orders_scraper.js":
                # with the upload token filled in, the exporter sends orders straight here, no file shuffling
                return self._send(200, self._script(os.environ.get("GHOSTSIGNAL_UPLOAD_TOKEN", ""), False),
                                  "text/javascript; charset=utf-8")
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            conn = db.connect(db_path)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/refresh/status":
                    return self._send(200, {"running": is_running(), "last_run": db.get_setting(conn, "last_run")})
                if url.path == "/api/blocked-brands":
                    return self._send(200, {"brands": restrictions.parse_list(db.get_setting(conn, restrictions.SETTING))})
                if url.path == "/api/invites":
                    return self._send(200, invites.listing(conn))
                if url.path == "/api/stores/budget":
                    from . import stores
                    return self._send(200, {"configured": stores.configured(),
                                            "left": stores.searches_left() if stores.configured() else None})
                if url.path == "/api/holidays":
                    from . import holidays
                    return self._send(200, holidays.upcoming())
                if url.path == "/api/ungate":
                    if time.time() - _ungate_cache["at"] > LIST_TTL or _ungate_cache["data"] is None:
                        _ungate_cache.update(at=time.time(), data=ungate.targets(conn))
                    return self._send(200, _ungate_cache["data"])
                if url.path == "/api/outcomes":
                    return self._send(200, db.outcomes(conn))
                if url.path == "/api/products":
                    archived = bool(qs.get("archived"))
                    full = bool(qs.get("full"))
                    key = (archived, full)
                    if _list_cache["key"] == key and time.time() - _list_cache["at"] < LIST_TTL:
                        rows = list(_list_cache["rows"])
                    else:
                        view = engine.product_view if full else engine.list_view
                        rows = [view(conn, r[0]) for r in conn.execute(
                            f"SELECT asin FROM products WHERE status {'=' if archived else '!='} 'dead'")]
                        _list_cache.update(key=key, at=time.time(), rows=list(rows))
                    if qs.get("verdict"):
                        rows = [r for r in rows if r["verdict"] == qs["verdict"].upper()]
                    if qs.get("q"):
                        q = qs["q"].lower()
                        rows = [r for r in rows if q in f"{r['asin']} {r['title']} {r['brand']}".lower()]
                    rows.sort(key=lambda r: -(r.get("priority") if r.get("priority") is not None else engine.priority(r)))
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
                        "last_run": db.get_setting(conn, "last_run"),
                        "spend": db.spend_by_buyer(conn),
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
                    return self._send(200, engine.recent_changes(conn))
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

        def _invite_label(self):
            code = parse_qs(urlparse(self.path).query).get("c", [""])[0]
            conn = db.connect(db_path)
            try:
                return code, invites.label_for(conn, code)
            finally:
                conn.close()

        def _script(self, token: str, invited: bool) -> bytes:
            s = (root / "tools" / "amazon_orders_scraper.js").read_text()
            if token:
                s = s.replace("__GS_URL__", self._public_url()).replace("__GS_TOKEN__", token)
            return s.replace("__GS_INVITED__", "1" if invited else "").encode()

        def _invite_get(self, path):
            """Public pages for invited friends. Everything here needs a valid invite code except the page shell."""
            if path == "/contribute":
                return self._send(200, (WEB / "contribute.html").read_bytes(), "text/html; charset=utf-8")
            code, label = self._invite_label()
            if not label:
                return self._send(404, {"error": "invite not found"})
            if path == "/api/invite/check":
                return self._send(200, {"label": label})
            if path == "/tools/amazon_orders_scraper.js":
                return self._send(200, self._script(code, True), "text/javascript; charset=utf-8")
            ext = root / "tools" / "extension"
            manifest = {
                "manifest_version": 3, "name": "GhostSignal Order Exporter", "version": "1.0",
                "description": "Send your Amazon order history (products only) to the GhostSignal database you were invited to.",
                "action": {"default_popup": "popup.html", "default_title": "GhostSignal"},
                "permissions": ["activeTab", "scripting"],
                "host_permissions": ["https://www.amazon.com/*", self._public_url() + "/*"],
            }
            config = "const GS_CONFIG = " + json.dumps({"server": self._public_url(), "invite": code, "label": label}) + ";\n"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("ghostsignal-exporter/manifest.json", json.dumps(manifest, indent=2))
                z.writestr("ghostsignal-exporter/config.js", config)
                z.writestr("ghostsignal-exporter/scraper.js", self._script(code, True))
                for name in ("popup.html", "popup.js"):
                    z.write(ext / name, f"ghostsignal-exporter/{name}")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="ghostsignal-exporter.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None

        def _ingest(self, url):
            """Orders pushed by the exporter, the extension or the contribute page. The upload token or an
            invite code only allows adding orders. Invite uploads are always filed under the invite's label."""
            token = os.environ.get("GHOSTSIGNAL_UPLOAD_TOKEN", "")
            given = parse_qs(url.query).get("token", [""])[0]
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) or b"{}"
            owner = bool(token) and hmac.compare_digest(given.encode(), token.encode())
            conn = db.connect(db_path)
            try:
                invite_label = None if owner else invites.label_for(conn, given)
            finally:
                conn.close()
            if not owner and not invite_label:
                return self._json_cors(401, {"error": "bad token"})
            try:
                text = raw.decode("utf-8-sig", errors="replace")
                is_json = text.lstrip()[:1] in ("{", "[")
                data = json.loads(text) if is_json else None
                buyer = invite_label or ((data.get("buyer") or "me").strip() if isinstance(data, dict) else "me")
                with tempfile.NamedTemporaryFile("w", suffix=".json" if is_json else ".csv", delete=False, encoding="utf-8") as f:
                    f.write(text)
                conn = db.connect(db_path)
                try:
                    r = importers.import_orders(conn, f.name, buyer)
                finally:
                    conn.close()
                    Path(f.name).unlink(missing_ok=True)
                refresh_in_background(db_path)
                return self._json_cors(200, {**r, "buyer": buyer})
            except (ValueError, KeyError) as e:
                return self._json_cors(400, {"error": str(e)})

        def _json_cors(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            _invalidate_lists()
            url = urlparse(self.path)
            if url.path == "/login":
                return self._login()
            if url.path == "/api/ingest/orders":
                return self._ingest(url)
            if not self._authed():
                return None
            if url.path == "/mcp":
                try:
                    msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                batch = isinstance(msg, list)
                replies = [r for r in (mcp.handle(db_path, m) for m in (msg if batch else [msg])) if r is not None]
                if not replies:  # only notifications
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._send(200, replies if batch else replies[0])
            conn = db.connect(db_path)
            try:
                body = self._body()
                if url.path == "/api/invites":
                    code = invites.create(conn, body.get("label") or "")
                    conn.commit()
                    return self._send(200, {"code": code, "url": f"{self._public_url()}/contribute?c={code}"})
                if url.path == "/api/approvals":
                    r = ungate.set_approval(conn, body["name"], body["status"], body.get("note"))
                    conn.commit()
                    if body["status"] == "approved":
                        refresh_in_background(db_path)
                    return self._send(200, r)
                if url.path == "/api/invites/revoke":
                    ok = invites.revoke(conn, body.get("code") or "")
                    conn.commit()
                    return self._send(200, {"ok": ok})
                if url.path in ("/api/products/archive", "/api/products/restore"):
                    n = db.archive_products(conn, body.get("asins") or [], restore=url.path.endswith("restore"))
                    conn.commit()
                    return self._send(200, {"ok": True, "count": n})
                if url.path == "/api/blocked-brands":
                    brands = restrictions.parse_list(body.get("brands") if isinstance(body.get("brands"), str)
                                                     else "\n".join(body.get("brands") or []))
                    db.set_setting(conn, restrictions.SETTING, "\n".join(brands))
                    conn.commit()
                    return self._send(200, {"brands": brands})
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
                    _, _, _, _, sig = engine.evaluate(conn, m.group(1))
                    db.add_inventory(conn, m.group(1), int(body.get("qty") or 1),
                                     float(body["unit_cost"]) if body.get("unit_cost") not in (None, "") else None,
                                     body.get("store") or "", body.get("channel") or "",
                                     float(body["list_price"]) if body.get("list_price") not in (None, "") else None,
                                     pred={"verdict": sig.verdict, "score": sig.score, "profit": sig.economics.profit})
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
                if url.path == "/api/holidays/pull":
                    from . import holidays
                    kws = holidays.keywords_for(body.get("name") or "")
                    if not kws:
                        return self._send(400, {"error": "unknown holiday"})
                    if is_running():
                        return self._send(200, {"started": False, "running": True})
                    discover_in_background(db_path, keywords=kws, tag=f"holiday:{body['name']}", pages=2)
                    return self._send(200, {"started": True, "running": True})
                if url.path == "/api/discover":
                    if is_running():
                        return self._send(200, {"started": False, "running": True})
                    discover_in_background(db_path, groups=body.get("groups"), brands=body.get("brands"),
                                           keywords=body.get("keywords"), pages=int(body.get("pages") or 2))
                    return self._send(200, {"started": True, "running": True})
                if url.path == "/api/refresh":      # pull fresh data from every connected source, in the background
                    if is_running():
                        return self._send(200, {"started": False, "running": True})
                    refresh_in_background(db_path, 0 if body.get("force") else None)   # force = re-pull everything now
                    return self._send(200, {"started": True, "running": True})
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
