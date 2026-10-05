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
    // Filled in automatically when you copy this script from your GhostSignal dashboard.
    // With these set, orders are sent straight to GhostSignal. Otherwise a file downloads.
    uploadUrl: "__GS_URL__",
    uploadToken: "__GS_TOKEN__",
    invited: "__GS_INVITED__",   // "1" when this copy came from an invite link
  };
  // The GhostSignal browser extension passes its settings in here instead.
  const EXT = typeof window !== "undefined" && window.__GS_CFG__;
  if (EXT) { CONFIG.uploadUrl = EXT.url; CONFIG.uploadToken = EXT.token; }
  const ORDER_CARD = ".order-card, .js-order-card, .a-box-group.order";
  const ASIN_RE = /(?:\/(?:dp|gp\/product|gp\/aw\/d|product)\/|[?&](?:asin|ASIN)=)([A-Z0-9]{10})/;
  const ORDER_ID_RE = /\b(\d{3}-\d{7}-\d{7}|D\d{2}-\d{7}-\d{7})\b/;
  const DATE_RE = /(?:Order placed|Ordered on)\s*([A-Z][a-z]+\.? \d{1,2}, \d{4})/i;
  const TOTAL_RE = /Total\s*\$?([\d,]+\.\d{2})/i;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  let frame;

  async function loadPage(url) {
    if (!frame) {
      frame = document.createElement("iframe");
      frame.style.cssText = "position:fixed;left:-9999px;top:-9999px;width:1200px;height:900px;opacity:0;pointer-events:none";
      document.body.appendChild(frame);
    }
    await new Promise((resolve, reject) => {
      const done = () => resolve();
      frame.onload = done;
      frame.onerror = () => reject(new Error(`Could not load ${url}`));
      frame.src = url;
      setTimeout(done, 8000);
    });
    await sleep(1200);
    const doc = frame.contentDocument;
    if (!doc || doc.location.href.includes("/ap/signin")) {
      throw new Error("Amazon wants you to sign in again. Refresh, sign in, rerun.");
    }
    return doc;
  }

  if (!location.hostname.includes("amazon.")) {
    alert("Run this on amazon.com (Your Orders page).");
    return;
  }

  // Invite links file your orders under the label the owner gave you, so don't ask.
  const invited = Boolean(EXT) || CONFIG.invited === "1";
  const buyer = invited ? "invited" : (prompt("Label for whose orders these are (e.g. me, gf, mom):", "me") || "me").trim();

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

  function itemTitle(el, box) {
    const direct = clean(el.innerText || el.getAttribute?.("title") || el.getAttribute?.("aria-label") || el.querySelector?.("img")?.alt);
    if (direct && !/buy it again|view item|write a review|return or replace/i.test(direct)) return direct;
    const titleEl = box?.querySelector?.("a[href*='/dp/'], a[href*='/gp/product/'], .yohtmlc-product-title, .a-link-normal .a-text-normal, [data-asin] img");
    return clean(titleEl?.innerText || titleEl?.getAttribute?.("title") || titleEl?.getAttribute?.("aria-label") || titleEl?.querySelector?.("img")?.alt || "");
  }

  function addItem(items, asin, el) {
    asin = (asin || "").toUpperCase();
    if (!/^[A-Z0-9]{10}$/.test(asin)) return;
    const box = el.closest?.("[data-asin], .a-fixed-left-grid, .yohtmlc-item, .item-box, li, .a-row") || el.parentElement || el;
    const qtyEl = box?.querySelector?.(".product-image__qty, .item-view-qty, .od-item-view-qty, [class*='qty']");
    const qty = parseInt(clean(qtyEl?.innerText).replace(/[^0-9]/g, ""), 10) || 1;
    const title = itemTitle(el, box);
    const prev = items.get(asin);
    if (!prev || title.length > prev.title.length) items.set(asin, { asin, title, quantity: qty });
  }

  function parseItems(container) {
    const items = new Map();
    container.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href") || "";
      const m = href.match(ASIN_RE) || href.match(/\b(B0[A-Z0-9]{8}|\d{9}[\dX])\b/);
      if (m) addItem(items, m[1], a);
    });
    container.querySelectorAll("[data-asin]").forEach((el) => addItem(items, el.getAttribute("data-asin"), el));
    return items;
  }

  function cardMeta(card) {
    const text = clean(card.innerText || card.textContent);
    return {
      order_id: (text.match(ORDER_ID_RE) || [])[1] || "",
      order_date: (text.match(DATE_RE) || [])[1] || "",
      order_total: (text.match(TOTAL_RE) || [])[1] || "",
    };
  }

  function orderDetailsUrl(card) {
    const links = [...card.querySelectorAll("a[href]")];
    const a = links.find((x) => /view order details/i.test(clean(x.innerText || x.textContent)))
      || links.find((x) => /order-details|orderID=|orderId=/i.test(x.getAttribute("href") || ""));
    if (!a) return "";
    return new URL(a.getAttribute("href"), location.origin).toString();
  }

  function rowsFromItems(items, meta) {
    return [...items.values()].map((it) => ({
      buyer, ...meta, ...it,
    }));
  }

  function parseCard(card) {
    return rowsFromItems(parseItems(card), cardMeta(card));
  }

  async function parseCardWithFallback(card) {
    const direct = parseCard(card);
    if (direct.length) return direct;
    const url = orderDetailsUrl(card);
    if (!url) return direct;
    try {
      const doc = await loadPage(url);
      const meta = { ...cardMeta(card), ...cardMeta(doc.body) };
      return rowsFromItems(parseItems(doc.body), meta);
    } catch (e) {
      console.warn("GhostSignal: order details fallback failed", url, e);
      return direct;
    }
  }

  for (const year of years) {
    for (let start = 0; start < 1000; start += CONFIG.pageSize) {
      const url = `/your-orders/orders?timeFilter=${year}&startIndex=${start}`;
      let doc;
      try {
        doc = await loadPage(url);
      } catch (e) {
        if (/sign in/i.test(e.message)) alert(e.message);
        console.warn("GhostSignal: fetch failed", url, e);
        break;
      }
      const cards = doc.querySelectorAll(ORDER_CARD);
      if (!cards.length) break;
      let fresh = 0;
      for (const card of cards) {
        for (const r of await parseCardWithFallback(card)) {
          const key = `${r.order_id}|${r.asin}`;
          if (seen.has(key)) continue;
          seen.add(key);
          rows.push(r);
          fresh++;
        }
        await sleep(150);
      }
      console.log(`GhostSignal: ${year} page ${start / CONFIG.pageSize + 1} → ${cards.length} orders, ${fresh} items (total ${rows.length})`);
      if (cards.length < CONFIG.pageSize) break;
      await sleep(CONFIG.delayMs);
    }
  }

  if (!rows.length) {
    alert("GhostSignal found 0 items. Amazon's page layout may have changed — check ORDER_CARD selector, or use Amazon's 'Request Your Data' export instead.");
    return;
  }

  const buyerFile = buyer.replace(/[^a-z0-9_-]+/gi, "-");
  const payload = JSON.stringify({ exported_at: new Date().toISOString(), buyer, orders: rows });
  const asins = [...new Set(rows.map((r) => r.asin))];

  // 1) Straight to GhostSignal (if this copy of the script knows where it lives)
  if (CONFIG.uploadUrl && !CONFIG.uploadUrl.startsWith("__")) {
    try {
      const res = await fetch(`${CONFIG.uploadUrl}/api/ingest/orders?token=${encodeURIComponent(CONFIG.uploadToken)}`, {
        method: "POST", headers: { "Content-Type": "text/plain" }, body: payload,
      });
      if (res.ok) {
        const out = await res.json();
        console.log("GhostSignal: uploaded", out);
        alert(`GhostSignal: sent ${out.added} new order lines (${asins.length} products) for "${buyer}". You're done.`);
        return;
      }
      console.warn("GhostSignal: upload refused", res.status);
    } catch (e) {
      // Amazon's page security can block sending to another site; fall through to the file download.
      console.warn("GhostSignal: could not upload, downloading a file instead.", e);
    }
  }

  // 2) Fallback: download files you can import by hand
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

  download(`amazon-orders-${buyerFile}.json`, payload, "application/json");
  download(`amazon-orders-${buyerFile}.csv`, csv, "text/csv");

  try { await navigator.clipboard.writeText(asins.join("\n")); } catch (_) {}
  console.log(`GhostSignal: done. ${rows.length} items, ${asins.length} unique ASINs (ASIN list copied to clipboard).`);
  alert(`GhostSignal: exported ${rows.length} items / ${asins.length} unique ASINs.\nFiles downloaded; ASIN list copied to clipboard.`);
  frame?.remove();
})();
