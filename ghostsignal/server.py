"""Dashboard server — stdlib only. `gs serve` then open http://127.0.0.1:8787"""

from __future__ import annotations

import json
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, engine, importers

WEB = Path(__file__).parent / "web"
ASIN_PATH = re.compile(r"^/api/products/([A-Z0-9]{10})(?:/(source|status|snapshot))?$")
ORDER = {"BUY": 2, "RESEARCH": 1, "PASS": 0}


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
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
            url = urlparse(self.path)
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
                    })
                if url.path == "/api/asins":
                    rows = conn.execute("SELECT asin FROM products WHERE status NOT IN ('dead','pass')").fetchall()
                    return self._send(200, "\n".join(r[0] for r in rows).encode(), "text/plain")
                return self._send(404, {"error": "not found"})
            except KeyError:
                return self._send(404, {"error": "unknown asin"})
            finally:
                conn.close()

        def do_POST(self):
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
                if m and m.group(2) == "snapshot":
                    db.add_snapshot(conn, m.group(1), "manual", **{k: v for k, v in body.items() if v not in ("", None)})
                    conn.commit()
                    return self._send(200, engine.product_view(conn, m.group(1)))
                if url.path == "/api/add":
                    return self._send(200, {"added": importers.import_asin_text(conn, body.get("text", ""))})
                if url.path in ("/api/import/orders", "/api/import/products"):
                    suffix = Path(body.get("filename") or "upload.csv").suffix or ".csv"
                    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as f:
                        f.write(body["content"])
                    try:
                        if url.path.endswith("orders"):
                            r = importers.import_orders(conn, f.name, body.get("buyer") or "me")
                        else:
                            r = importers.import_products(conn, f.name, body.get("source") or "upload")
                    finally:
                        Path(f.name).unlink(missing_ok=True)
                    return self._send(200, r)
                if url.path == "/api/score":
                    changes = engine.run(conn)
                    return self._send(200, {"changes": [
                        {"asin": c["asin"], "title": c["title"], "kind": c["kind"],
                         "score": c["signal"].score, "verdict": c["signal"].verdict} for c in changes]})
                return self._send(404, {"error": "not found"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            finally:
                conn.close()

    return Handler


def serve(db_path=None, host="127.0.0.1", port=8787):
    db.connect(db_path).close()
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"GhostSignal dashboard → http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
