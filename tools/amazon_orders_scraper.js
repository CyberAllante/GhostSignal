/*
 * GhostSignal — Amazon order history exporter
 * -------------------------------------------
 * Exports YOUR OWN order history (ASIN, title, date, order #, qty) from amazon.com.
 * Runs entirely in your browser, logged in as you. Nothing is sent anywhere —
 * it downloads a JSON + CSV file you then import with:
 *
 *     gs import-orders ~/Downloads/amazon-orders-me.json --buyer me
 *
 * HOW TO USE
 *   1. Log in to amazon.com and open https://www.amazon.com/your-orders/orders
 *   2. Open DevTools console (Cmd+Opt+J on Mac / Ctrl+Shift+J on Windows)
 *   3. Paste this whole file, press Enter. (Chrome may ask you to type "allow pasting" first.)
 *   4. Wait — it walks every year it can see, ~1.5s per page to stay polite.
 *
 * Friends/family: send them this file + instructions. They run it on THEIR
 * account and send you back the JSON. No passwords ever change hands.
 *
 * Amazon changes its HTML from time to time. If you get 0 orders, the
 * selectors below (ORDER_CARD, etc.) are the place to adjust.
 */
(async () => {
  const CONFIG = {
    maxYears: 6,          // how far back to go
    delayMs: 1500,        // pause between page loads
    pageSize: 10,         // Amazon shows 10 orders per page
  };
  const ORDER_CARD = ".order-card, .js-order-card, .a-box-group.order";
  const ASIN_RE = /\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})/;
  const ORDER_ID_RE = /\b(\d{3}-\d{7}-\d{7}|D\d{2}-\d{7}-\d{7})\b/;
  const DATE_RE = /(?:Order placed|Ordered on)\s*([A-Z][a-z]+\.? \d{1,2}, \d{4})/i;
  const TOTAL_RE = /Total\s*\$?([\d,]+\.\d{2})/i;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

  if (!location.hostname.includes("amazon.")) {
    alert("Run this on amazon.com (Your Orders page).");
    return;
  }

  const buyer = (prompt("Label for whose orders these are (e.g. me, gf, mom):", "me") || "me").trim();

  // Which years are available? Read the time filter dropdown, else fall back.
  let years = [...document.querySelectorAll('select[name="timeFilter"] option, #time-filter option')]
    .map((o) => o.value)
    .filter((v) => /^year-\d{4}$/.test(v));
  if (!years.length) {
    const y = new Date().getFullYear();
    years = Array.from({ length: CONFIG.maxYears }, (_, i) => `year-${y - i}`);
  }
  years = years.slice(0, CONFIG.maxYears);

  const parser = new DOMParser();
  const rows = [];
  const seen = new Set();

  function parseCard(card) {
    const text = clean(card.innerText || card.textContent);
    const orderId = (text.match(ORDER_ID_RE) || [])[1] || "";
    const date = (text.match(DATE_RE) || [])[1] || "";
    const total = (text.match(TOTAL_RE) || [])[1] || "";
    const items = new Map();
    card.querySelectorAll("a[href]").forEach((a) => {
      const m = a.getAttribute("href").match(ASIN_RE);
      if (!m) return;
      const asin = m[1];
      const title = clean(a.innerText || a.getAttribute("title") || a.querySelector("img")?.alt);
      const prev = items.get(asin);
      if (!prev || title.length > prev.title.length) {
        // quantity badge sits next to the product image on most layouts
        const box = a.closest(".a-fixed-left-grid, .yohtmlc-item, .item-box, li, .a-row") || a.parentElement;
        const qtyEl = box?.querySelector(".product-image__qty, .item-view-qty, .od-item-view-qty");
        const qty = parseInt(clean(qtyEl?.innerText), 10) || 1;
        items.set(asin, { asin, title, quantity: qty });
      }
    });
    return [...items.values()].map((it) => ({
      buyer, order_id: orderId, order_date: date, order_total: total, ...it,
    }));
  }

  for (const year of years) {
    for (let start = 0; start < 1000; start += CONFIG.pageSize) {
      const url = `/your-orders/orders?timeFilter=${year}&startIndex=${start}`;
      let doc;
      try {
        const res = await fetch(url, { credentials: "include" });
        if (res.url.includes("/ap/signin")) { alert("Amazon wants you to sign in again. Refresh, sign in, rerun."); return; }
        doc = parser.parseFromString(await res.text(), "text/html");
      } catch (e) {
        console.warn("GhostSignal: fetch failed", url, e);
        break;
      }
      const cards = doc.querySelectorAll(ORDER_CARD);
      if (!cards.length) break;
      let fresh = 0;
      cards.forEach((card) => {
        for (const r of parseCard(card)) {
          const key = `${r.order_id}|${r.asin}`;
          if (seen.has(key)) continue;
          seen.add(key);
          rows.push(r);
          fresh++;
        }
      });
      console.log(`GhostSignal: ${year} page ${start / CONFIG.pageSize + 1} → ${cards.length} orders, ${fresh} items (total ${rows.length})`);
      if (cards.length < CONFIG.pageSize) break;
      await sleep(CONFIG.delayMs);
    }
  }

  if (!rows.length) {
    alert("GhostSignal found 0 items. Amazon's page layout may have changed — check ORDER_CARD selector, or use Amazon's 'Request Your Data' export instead.");
    return;
  }

  const download = (name, body, type) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([body], { type }));
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  const cols = ["buyer", "order_id", "order_date", "asin", "title", "quantity", "order_total"];
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const csv = [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");

  download(`amazon-orders-${buyer}.json`, JSON.stringify({ exported_at: new Date().toISOString(), buyer, orders: rows }, null, 2), "application/json");
  download(`amazon-orders-${buyer}.csv`, csv, "text/csv");

  const asins = [...new Set(rows.map((r) => r.asin))];
  try { await navigator.clipboard.writeText(asins.join("\n")); } catch (_) {}
  console.log(`GhostSignal: done. ${rows.length} items, ${asins.length} unique ASINs (ASIN list copied to clipboard).`);
  alert(`GhostSignal: exported ${rows.length} items / ${asins.length} unique ASINs.\nFiles downloaded; ASIN list copied to clipboard.`);
})();
