import json

import pytest

from ghostsignal import db, engine, importers, keepa
from ghostsignal.scoring import economics, score_product


@pytest.fixture
def conn():
    return db.connect(":memory:")


PRIVACY_CSV = '''"Website","Order ID","Order Date","Currency","Unit Price","Quantity","ASIN","Order Status","Shipping Address","Product Name"
"Amazon.com","111-1234567-1234567","2025-03-01T18:22:11Z","USD","12.99","2","B00NLVM6WK","Closed","123 Secret St","Wild Planet Tuna"
"Amazon.com","111-1234567-7654321","2025-04-02T10:00:00Z","USD","5.00","1","B06W9N8X9H","Cancelled","123 Secret St","TJ Bagel"
'''


def test_import_privacy_export_drops_cancelled_and_addresses(conn, tmp_path):
    f = tmp_path / "Retail.OrderHistory.1.csv"
    f.write_text(PRIVACY_CSV)
    r = importers.import_orders(conn, f, "me")
    assert r["added"] == 1
    row = dict(conn.execute("SELECT * FROM orders").fetchone())
    assert row["asin"] == "B00NLVM6WK" and row["quantity"] == 2 and row["unit_price"] == 12.99
    assert row["order_date"] == "2025-03-01"
    assert "Secret" not in json.dumps(row)
    # re-import is idempotent
    assert importers.import_orders(conn, f, "me")["added"] == 0


def test_import_scraper_json(conn, tmp_path):
    f = tmp_path / "amazon-orders-gf.json"
    f.write_text(json.dumps({"buyer": "gf", "orders": [
        {"order_id": "112-0000000-0000001", "order_date": "March 4, 2025", "asin": "B00F0FC3OC",
         "title": "Pocky", "quantity": 1},
    ]}))
    assert importers.import_orders(conn, f, "gf")["added"] == 1
    assert conn.execute("SELECT order_date FROM orders").fetchone()[0] == "2025-03-04"
    assert db.order_stats(conn, "B00F0FC3OC")["buyers"] == 1


def test_import_stealthseller_style_csv(conn, tmp_path):
    f = tmp_path / "ss.csv"
    f.write_text("ASIN,Title,Buy Box,Sales Rank,Monthly Sold,Offers,Avg Price,Source URL,Cost\n"
                 "B00F0FC3OC,Pocky,$30.89,21.2K,<50,14,$29.40,https://www.costco.com/pocky.html,$10.99\n")
    r = importers.import_products(conn, f, "stealthseller")
    assert r == {"rows": 1, "products": 1, "snapshots": 1, "sources": 1}
    s = db.latest_snapshot(conn, "B00F0FC3OC")
    assert (s["buy_box"], s["sales_rank"], s["monthly_sold"], s["offer_count"]) == (30.89, 21200, 25, 14)
    assert db.latest_sources(conn, "B00F0FC3OC")[0]["retailer"] == "costco"


def test_asin_text_extraction(conn):
    n = importers.import_asin_text(conn, "see https://www.amazon.com/dp/B06W9N8X9H/ref=x and b00nlvm6wk, B06W9N8X9H")
    assert n == 2


def test_economics_grocery_referral_and_max_cost():
    e = economics(12.0, 3.0, "Grocery & Gourmet Food", fba_fee=3.0)
    assert e.referral_fee == 0.96          # 8% at or under $15
    assert e.net_payout == round(12 - 0.96 - 3.0 - 0.60, 2)
    assert e.max_cost == round(e.net_payout / 1.3, 2)
    assert e.profit == round(e.net_payout - 3.0, 2)


def test_verdicts():
    snap = {"buy_box": 30.0, "avg_price_90": 29.0, "monthly_sold": 500, "offer_count": 5, "fba_fee": 4.0}
    good = score_product({}, snap, [{"retailer": "costco", "price": 10.0, "pack_qty": 1, "in_stock": 1}],
                         {"orders": 3, "buyers": 3, "repeat_buyers": 1})
    assert good.verdict == "BUY" and good.score >= 70
    no_cost = score_product({}, snap, [], {"orders": 0})
    assert no_cost.verdict == "RESEARCH" and any("buy under" in f for f in no_cost.flags)
    loser = score_product({}, snap, [{"retailer": "x", "price": 28.0, "pack_qty": 1}], {"orders": 0})
    assert loser.verdict == "PASS"
    oos = score_product({}, snap, [{"retailer": "x", "price": 1.0, "pack_qty": 1, "in_stock": 0}], {"orders": 0})
    assert oos.economics.cost is None


def test_engine_alerts_on_upgrade(conn):
    db.upsert_product(conn, "B00F0FC3OC", title="Pocky")
    db.add_snapshot(conn, "B00F0FC3OC", "t", buy_box=30.0, avg_price_90=29.0, monthly_sold=500,
                    offer_count=5, fba_fee=4.0)
    db.add_source(conn, "B00F0FC3OC", "walmart", 29.0)
    assert engine.run(conn) == []                      # PASS, nothing to say
    db.add_source(conn, "B00F0FC3OC", "costco", 10.0, in_stock=1)
    changes = engine.run(conn)
    assert len(changes) == 1 and changes[0]["kind"] == "PASS → BUY"
    assert "BUY" in engine.format_alert(changes[0])


def test_keepa_parse():
    cur = [-1] * 20
    cur[keepa.AMAZON], cur[keepa.SALES_RANK], cur[keepa.COUNT_NEW], cur[keepa.BUY_BOX] = 2399, 2567, 18, 2399
    avg = [-1] * 20
    avg[keepa.BUY_BOX] = 1613
    prod, snap = keepa.parse_product({
        "asin": "B00NLVM6WK", "title": "Tuna", "brand": "Wild Planet", "imagesCSV": "abc.jpg,def.jpg",
        "categoryTree": [{"name": "Grocery & Gourmet Food"}], "upcList": ["123"], "monthlySold": 2000,
        "fbaFees": {"pickAndPackFee": 410}, "referralFeePercentage": 15,
        "stats": {"current": cur, "avg90": avg, "buyBoxPrice": 2399},
    })
    assert prod["image_url"].endswith("abc.jpg") and prod["category"].startswith("Grocery")
    assert snap == {"buy_box": 23.99, "amazon_price": 23.99, "avg_price_90": 16.13, "sales_rank": 2567,
                    "monthly_sold": 2000, "offer_count": 18, "referral_pct": 0.15, "fba_fee": 4.10}


def test_gating_overrides_verdict(conn):
    from ghostsignal import spapi
    snap = {"buy_box": 30.0, "avg_price_90": 29.0, "monthly_sold": 500, "offer_count": 5, "fba_fee": 4.0}
    src = [{"retailer": "costco", "price": 10.0, "pack_qty": 1, "in_stock": 1}]
    orders = {"orders": 3, "buyers": 3, "repeat_buyers": 1}
    assert score_product({}, snap, src, orders, eligibility={"status": "ungated"}).verdict == "BUY"
    assert score_product({}, snap, src, orders, eligibility={"status": "approval"}).verdict == "RESEARCH"
    assert score_product({}, snap, src, orders, eligibility={"status": "blocked"}).verdict == "PASS"
    # confirmed ungated ignores the AI's "likely gated" guess
    s = score_product({}, snap, src, orders, {"gating_risk": "high"}, eligibility={"status": "ungated"})
    assert s.verdict == "BUY" and s.gated == "ungated"

    assert spapi.parse_restrictions({"restrictions": []})[0] == "ungated"
    st, _, url = spapi.parse_restrictions({"restrictions": [{"reasons": [
        {"reasonCode": "APPROVAL_REQUIRED", "message": "Need approval",
         "links": [{"resource": "https://sellercentral.amazon.com/hz/approvalrequest?asin=X"}]}]}]})
    assert st == "approval" and url.startswith("https://sellercentral")
    assert spapi.parse_restrictions({"restrictions": [{"reasons": [{"reasonCode": "NOT_ELIGIBLE"}]}]})[0] == "blocked"
    assert spapi.parse_fees({"payload": {"FeesEstimateResult": {"FeesEstimate": {"FeeDetailList": [
        {"FeeType": "ReferralFee", "FinalFee": {"Amount": 4.63}},
        {"FeeType": "FBAFees", "FinalFee": {"Amount": 4.95}}]}}}}) == (4.63, 4.95)

    db.set_eligibility(conn, "B00F0FC3OC", "blocked")
    assert engine.product_view(conn, "B00F0FC3OC")["gated"] == "blocked"


def test_multichannel_best_and_blocked_amazon(conn):
    from ghostsignal.scoring import economics
    e = economics(30.0, 10.0, fba_fee=4.0, ebay_price=40.0, facebook_price=22.0)
    assert e.best_channel == "ebay" and e.channels["facebook"]["profit"] == 12.0
    assert e.profit == e.channels["ebay"]["profit"]
    # Can't sell on Amazon, but eBay is still a valid exit -> not forced to PASS
    snap = {"buy_box": 30.0, "monthly_sold": 500, "offer_count": 5, "fba_fee": 4.0}
    sig = score_product({}, snap, [{"retailer": "x", "price": 10.0, "pack_qty": 1, "in_stock": 1}],
                        {"orders": 0}, eligibility={"status": "blocked"}, channel_prices={"ebay": 40.0})
    assert sig.economics.best_channel == "ebay" and sig.verdict != "PASS"


def test_inventory_flow_and_custom_items(conn):
    item = db.new_item_id(conn)
    assert item == "GS00000001"
    db.upsert_product(conn, item, title="Pyrex bowls")
    db.set_channel_price(conn, item, "facebook", 60.0)
    lot = db.add_inventory(conn, item, 2, 15.0, "goodwill", "ebay", 80.0)
    assert db.inventory_rows(conn)[0]["status"] == "listed"
    db.update_inventory(conn, lot, channel="facebook", status="in_hand", list_price=None)
    db.update_inventory(conn, lot, status="sold", sold_price=55.0)
    row = db.inventory_rows(conn)[0]
    assert row["channel"] == "facebook" and row["sold_qty"] == 2 and row["profit"] == 80.0
    v = engine.product_view(conn, item)
    assert v["economics"]["best_channel"] == "facebook" and "amazon" not in v["links"]


def test_store_matching():
    from ghostsignal import stores
    assert [stores.store_key(x) for x in ("Walmart", "samsclub.com", "Sam's Club", "Amazon.com", "eBay - seller")] == \
        ["walmart", "samsclub", "samsclub", None, None]
    offers = stores.pick_offers([
        {"title": "Peet's Coffee Major Dickason Dark Roast", "source": "samsclub.com", "extracted_price": 23.98, "via": "lens"},
        {"title": "Peets Major Dickasons Blend Ground Coffee 32oz", "source": "Costco", "extracted_price": 24.99},
        {"title": "Totally different thing", "source": "Target", "extracted_price": 3.0},
    ], "Peet's Coffee Major Dickason's Blend Dark Roast Ground Coffee, 32 oz")
    assert [o["retailer"] for o in offers] == ["samsclub", "costco"]


def test_clear_demo_only_removes_samples(conn):
    from ghostsignal.demo import seed, clear_demo, has_demo
    seed(conn)
    db.upsert_product(conn, "B0REALITEM1", title="Mine", origin="orders")
    db.add_inventory(conn, "B0REALITEM1", 1, 5.0)
    assert has_demo(conn)
    assert clear_demo(conn) == 5
    assert not has_demo(conn)
    assert conn.execute("SELECT asin FROM products").fetchall()[0][0] == "B0REALITEM1"
    assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_server_requires_password(tmp_path, monkeypatch):
    import base64, threading, urllib.error, urllib.request
    from http.server import ThreadingHTTPServer
    from ghostsignal import server
    monkeypatch.setenv("GHOSTSIGNAL_PASSWORD", "pw")
    dbp = tmp_path / "t.db"
    db.connect(dbp).close()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(str(dbp)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        urllib.request.urlopen(base + "/api/stats")
        assert False, "should be 401"
    except urllib.error.HTTPError as e:
        assert e.code == 401
    assert urllib.request.urlopen(base + "/health").status == 200
    req = urllib.request.Request(base + "/api/stats", headers={"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()})
    assert urllib.request.urlopen(req).status == 200
    httpd.shutdown()


def test_dormant_pass_revives_and_refresh_cadence(conn):
    from datetime import datetime, timedelta, timezone
    asin = "B00F0FC3OC"
    db.upsert_product(conn, asin, title="Pocky")
    db.add_snapshot(conn, asin, "t", buy_box=30.0, avg_price_90=29.0, monthly_sold=500, offer_count=5, fba_fee=4.0)
    db.add_source(conn, asin, "walmart", 29.0)
    assert engine.record_signal(conn, asin) is None                       # PASS today
    old = (datetime.now(timezone.utc) - timedelta(days=200)).replace(microsecond=0).isoformat()
    conn.execute("UPDATE signals SET created_at = ?", (old,))             # ...and it's been a PASS for 200 days
    db.add_source(conn, asin, "costco", 10.0, in_stock=1)                 # a cheaper source appears
    change = engine.record_signal(conn, asin)
    assert change and change["kind"].startswith("REVIVED after 200d")
    # history rows are only written when something changed, so the table doesn't balloon
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    engine.record_signal(conn, asin)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == n
    assert engine.refresh_days("BUY", 80) < engine.refresh_days("RESEARCH", 50) < engine.refresh_days("PASS", 40) < engine.refresh_days("PASS", 10)


def test_mcp_tools(tmp_path):
    from ghostsignal import mcp
    from ghostsignal.demo import seed
    path = str(tmp_path / "m.db")
    c = db.connect(path)
    seed(c)
    engine.run(c)
    c.close()

    def call(name, **args):
        r = mcp.handle(path, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]
        return r["content"][0]["text"], r["isError"]

    init = mcp.handle(path, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
    assert init["serverInfo"]["name"] == "ghostsignal" and "analyst" in init["instructions"]
    assert mcp.handle(path, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert len(mcp.handle(path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]) >= 15
    text, bad = call("picks")
    assert not bad and text.splitlines()[0].startswith("B00F0FC3OC | BUY")
    text, _ = call("log_price", asin="B00NLVM6WK", retailer="walmart", price=9.0)       # rescored immediately
    assert "RESEARCH" in text or "BUY" in text
    text, _ = call("set_gated", asin="B00F0FC3OC", status="blocked")
    assert "CAN'T SELL" in text
    call("note", asin="B00F0FC3OC", text="Costco box is 10-ct")
    assert "Costco box is 10-ct" in call("product", asin="B00F0FC3OC")[0]
    assert call("set_gated", asin="B00F0FC3OC", status="nope")[1] is True            # bad input is an error, not a crash
    assert "error" in mcp.handle(path, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}})
    text, _ = call("import_orders", filename="amazon-orders-mom.json", content=json.dumps(
        {"orders": [{"order_id": "1", "asin": "B0NEWPROD1", "title": "New thing", "order_date": "2026-09-01", "quantity": 1}]}))
    assert "Imported 1 order lines for 'mom'" in text


def test_amazon_brands_forced_pass_and_archive(conn):
    db.upsert_product(conn, "B07BH5BYZ7", title="Amazon Basics 100-Pack AA Batteries", brand="Amazon Basics")
    db.upsert_product(conn, "B000000001", title="Some Cereal 12 oz", brand="Kelloggs")
    db.upsert_product(conn, "B000000002", title="Gadget", brand="Zorbo Labs")
    db.add_snapshot(conn, "B07BH5BYZ7", source="manual", buy_box=29.99, sales_rank=500, monthly_sold=3000, offer_count=2)
    p = engine.product_view(conn, "B07BH5BYZ7")
    assert p["verdict"] == "PASS" and p["score"] == 0 and "Amazon" in p["restricted"]
    assert engine.product_view(conn, "B000000001")["restricted"] == ""
    db.set_setting(conn, "blocked_brands", "Zorbo Labs")
    assert "blocked list" in engine.product_view(conn, "B000000002")["restricted"]
    assert db.archive_products(conn, ["B07BH5BYZ7"]) == 1
    assert conn.execute("SELECT status FROM products WHERE asin='B07BH5BYZ7'").fetchone()[0] == "dead"
    assert db.archive_products(conn, ["B07BH5BYZ7"], restore=True) == 1


def test_outcomes_compare_prediction_to_result(conn):
    db.upsert_product(conn, "B000000003", title="Thing", brand="Acme")
    i = db.add_inventory(conn, "B000000003", 2, 5.0, pred={"verdict": "BUY", "score": 80, "profit": 6.0})
    db.update_inventory(conn, i, status="sold", sold_price=12.0)
    o = db.outcomes(conn)
    assert o["by_verdict"]["BUY"]["wins"] == 1 and o["by_verdict"]["BUY"]["profit"] == 14.0 and o["total_profit"] == 14.0


CATALOG = {"items": [{"asin": "B00NLVM6WK",
    "summaries": [{"marketplaceId": "ATVPDKIKX0DER", "brand": "Wild Planet", "itemName": "Wild Planet Albacore Tuna 5oz",
                   "browseClassification": {"displayName": "Canned Tuna"}}],
    "salesRanks": [{"marketplaceId": "ATVPDKIKX0DER",
                    "displayGroupRanks": [{"title": "Grocery & Gourmet Food", "rank": 842}],
                    "classificationRanks": [{"title": "Canned Tuna", "rank": 12}]}],
    "images": [{"marketplaceId": "ATVPDKIKX0DER", "images": [{"variant": "PT01", "link": "x"}, {"variant": "MAIN", "link": "https://img/main.jpg"}]}]}]}
OFFERS = {"responses": [{"status": {"statusCode": 200}, "body": {"payload": {"ASIN": "B00NLVM6WK", "status": "Success",
    "Summary": {"TotalOfferCount": 9,
                "NumberOfOffers": [{"condition": "new", "fulfillmentChannel": "Amazon", "OfferCount": 3},
                                   {"condition": "new", "fulfillmentChannel": "Merchant", "OfferCount": 4},
                                   {"condition": "used", "fulfillmentChannel": "Merchant", "OfferCount": 2}],
                "BuyBoxPrices": [{"condition": "New", "LandedPrice": {"Amount": 24.5}, "ListingPrice": {"Amount": 24.5}}]},
    "Offers": [{"SellerId": "ATVPDKIKX0DER", "ListingPrice": {"Amount": 25.99}, "Shipping": {"Amount": 0}, "IsBuyBoxWinner": False},
               {"SellerId": "A1XYZ", "ListingPrice": {"Amount": 24.5}, "IsBuyBoxWinner": True}]}}},
    {"status": {"statusCode": 404}, "body": {"errors": []}}]}


def test_spapi_market_refresh(conn, monkeypatch):
    from ghostsignal import spapi
    monkeypatch.setattr(spapi.time, "sleep", lambda s: None)
    monkeypatch.setattr(spapi, "_call", lambda method, path, params=None, body=None: CATALOG if "catalog" in path else OFFERS)
    db.upsert_product(conn, "B00NLVM6WK")
    db.add_snapshot(conn, "B00NLVM6WK", "keepa", buy_box=20.0, monthly_sold=400, avg_price_90=23.0)
    assert spapi.refresh_market(conn, ["B00NLVM6WK", "GS00000001"]) == {"updated": 1, "not_found": 0}
    p = dict(conn.execute("SELECT * FROM products WHERE asin='B00NLVM6WK'").fetchone())
    assert p["brand"] == "Wild Planet" and p["image_url"] == "https://img/main.jpg" and p["category"] == "Grocery & Gourmet Food"
    s = dict(db.latest_snapshot(conn, "B00NLVM6WK"))
    assert s["buy_box"] == 24.5 and s["sales_rank"] == 842 and s["offer_count"] == 7
    assert s["fba_offers"] == 3 and s["fbm_offers"] == 4 and s["amazon_price"] == 25.99
    assert s["monthly_sold"] == 400 and s["avg_price_90"] == 23.0     # Keepa-only fields carried forward


def test_pin_login_and_lockout(tmp_path, monkeypatch):
    import threading, urllib.request, urllib.error, urllib.parse, http.server
    from ghostsignal import server
    monkeypatch.setenv("GHOSTSIGNAL_PASSWORD", "long-password-xyz")
    monkeypatch.setenv("GHOSTSIGNAL_PIN", "4321")
    server._pin_fails.update(n=0, locked_until=0.0)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(str(tmp_path / "t.db")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)

    def login(pw):
        req = urllib.request.Request(base + "/login", data=urllib.parse.urlencode({"password": pw}).encode())
        try:
            op.open(req)
        except urllib.error.HTTPError as e:
            return e.headers.get("Location"), e.headers.get("Set-Cookie")
    assert login("4321")[0] == "/"
    assert login("long-password-xyz")[0] == "/"
    for _ in range(5):
        assert login("0000")[1] is None
    assert login("4321")[0].startswith("/login?error")          # PIN locked after 5 bad PINs
    assert login("long-password-xyz")[0] == "/"                  # real password still works
    srv.shutdown()


def test_gift_cards_and_kindle_are_restricted():
    from ghostsignal import restrictions as r
    for t in ("Airbnb eGift Card - $100 - Standard", "Uber Gift Card - E-mail Delivery", "$10 XBOX Gift Card [Digital Code]",
              "Amazon Gift Card Balance Reload", "Best Buy Physical Gift Card", "Amazon Kindle (16 GB) - Lightest"):
        assert r.check({"title": t, "brand": ""}), t
    for t in ("Wyze Smart Scale X - Digital Bathroom Scale", "Starbucks Blonde Roast Iced Coffee", "Superer Micro USB Charger Cable Fit for Kindle Paperwhite",
              "simplehuman Code M 100 Count Custom Fit Liners"):
        assert not r.check({"title": t, "brand": ""}), t


def test_mcp_cleanup_and_outcomes(tmp_path):
    from ghostsignal import mcp
    path = tmp_path / "m.db"
    c = db.connect(str(path))
    db.upsert_product(c, "B07BH5BYZ7", title="Amazon Basics AA Batteries", brand="Amazon Basics")
    db.upsert_product(c, "B093Z1F4QM", title="Airbnb eGift Card - Standard")
    db.upsert_product(c, "B000000009", title="Real Product", brand="Acme")
    c.commit()
    prev = mcp.t_cleanup(c, {})
    assert "restricted: 2" in prev
    assert "Archived 2" in mcp.t_cleanup(c, {"archive": True, "groups": ["restricted"]})
    assert "Restored 2" in mcp.t_restore(c, {})
    assert "Zorbo" in mcp.t_blocked_brands(c, {"add": ["Zorbo"]})
    assert "Nothing sold" in mcp.t_outcomes(c, {})


def test_subcategory_rank_not_used_as_overall_rank():
    from ghostsignal import spapi
    item = {"asin": "B01N0W1YIK", "summaries": [{"marketplaceId": "ATVPDKIKX0DER", "brand": "MOSISO", "itemName": "Sleeve"}],
            "salesRanks": [{"marketplaceId": "ATVPDKIKX0DER", "displayGroupRanks": [],
                            "classificationRanks": [{"title": "Laptop Sleeves", "rank": 1}]}]}
    product, rank, extra = spapi.parse_catalog_item(item)
    assert rank is None and extra == {"sub_rank": 1, "sub_category": "Laptop Sleeves"} and product["category"] == "Laptop Sleeves"


def test_invite_flow(tmp_path, monkeypatch):
    import io as _io, threading, urllib.request, urllib.error, http.server, zipfile as _zip
    from ghostsignal import server, invites
    monkeypatch.setenv("GHOSTSIGNAL_PASSWORD", "long-password-xyz")
    monkeypatch.setenv("GHOSTSIGNAL_UPLOAD_TOKEN", "owner-token-123456789012345")
    path = str(tmp_path / "i.db")
    c = db.connect(path)
    code = invites.create(c, "Jay!")
    c.commit()
    c.close()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda u: urllib.request.urlopen(base + u)
    assert json.loads(get(f"/api/invite/check?c={code}").read())["label"] == "jay"
    assert b"contribute" not in get("/contribute").read()[:0] and get("/contribute").status == 200
    with pytest.raises(urllib.error.HTTPError):
        get("/api/invite/check?c=nope")
    z = _zip.ZipFile(_io.BytesIO(get(f"/extension.zip?c={code}").read()))
    names = z.namelist()
    assert "ghostsignal-exporter/manifest.json" in names and code in z.read("ghostsignal-exporter/config.js").decode()
    body = json.dumps({"buyer": "someone-else", "orders": [{"asin": "B00NLVM6WK", "title": "Tuna", "order_id": "111-1", "quantity": 1}]}).encode()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(f"{base}/api/ingest/orders?token={code}", data=body)).read())
    assert r["added"] == 1 and r["buyer"] == "jay"            # filed under the invite label, not what the file claims
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(urllib.request.Request(f"{base}/api/ingest/orders?token=bad", data=body))
    with pytest.raises(urllib.error.HTTPError):               # an invite can't read anything
        get(f"/api/products?token={code}")
    srv.shutdown()


def test_approval_tracking_clears_gating_on_approve(conn):
    from ghostsignal import ungate
    db.upsert_product(conn, "B000000011", title="Cereal", brand="Kelloggs")
    db.set_eligibility(conn, "B000000011", "approval", "spapi", "You need approval to list in the Grocery & Gourmet Foods category.")
    t = ungate.targets(conn)
    assert t["categories"][0]["name"] == "Grocery & Gourmet Foods" and t["categories"][0]["tracking"]["status"] == "not_started"
    ungate.set_approval(conn, "Grocery & Gourmet Foods", "applying", "KeHE invoice sent")
    assert ungate.targets(conn)["categories"][0]["tracking"]["note"] == "KeHE invoice sent"
    ungate.set_approval(conn, "Grocery & Gourmet Foods", "approved")
    assert db.get_eligibility(conn, "B000000011") is None      # cleared so the next run re-checks it


def test_list_view_is_slim(conn):
    db.upsert_product(conn, "B000000012", title="Thing", brand="Acme")
    v = engine.list_view(conn, "B000000012")
    assert set(v) >= {"asin", "verdict", "gated", "economics", "snapshot", "orders"} and "history" not in v and "links" not in v
