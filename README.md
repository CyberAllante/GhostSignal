# GHOSTSIGNAL

**Find the signal. Make the move.**

GhostSignal is a retail arbitrage intelligence system. Instead of hunting for one winning product, it keeps a growing database of every product you come across. It re-checks those products on a schedule, scores them, and alerts you when something becomes worth buying.

```
DATA  →  DETECT  →  VERIFY  →  SCORE  →  ALERT
```

## One master database, three ways to sell

Every product lives in one list, whether you found it on Amazon, at a thrift store or on Facebook. Each product gets a profit on **Amazon**, **eBay** and **Facebook Marketplace**, and the score uses the best one. Things you buy go into **Inventory**, where you can list them on any channel and move them between channels. If you can't sell something on Amazon but it sells on eBay, it isn't marked Pass.

## Where products come from

| Signal | How it gets in |
|---|---|
| **Your network's Amazon orders** (you, your partner, family, friends who opt in) | Browser exporter `tools/amazon_orders_scraper.js` or Amazon's "Request Your Data" export, loaded with `gs import-orders` |
| **Stealth Seller / Keepa / SellerAmp finds** | Export CSV, then `gs import-products --source stealthseller` |
| **Seller storefronts you shadow** | `gs seller add <SELLER_ID>` (Keepa API) |
| **Anything else** (TikTok tips, store scans, tips from people you know) | `gs add <ASIN or URL>` or paste in the dashboard |

For each product it tracks Amazon price, rank, offers and fees (Keepa or manual), your retail cost by store, demand from your own network, and AI risk flags. It then works out **profit, ROI, Max Cost and a 0–100 score**, with a verdict of **BUY / RESEARCH / PASS** and the reasons behind it.

History is never overwritten. Each check adds a new snapshot, so the system can notice that "this was a PASS in August and is a BUY now".

## Quick start

**On a Mac? Follow [SETUP.md](SETUP.md) step by step.** Run `gs setup` anytime to see what's connected.

```bash
./gs demo                   # loads sample products (nothing to install)
./gs serve                  # dashboard at http://127.0.0.1:8787
```

Or `pip install -e .` to get a `gs` command on your PATH.

Optional extras:

```bash
pip install -e '.[ai]'                 # Claude enrichment (gating, hazmat, IP-risk flags)
export KEEPA_API_KEY=...               # live Amazon data and seller tracking
export ANTHROPIC_API_KEY=...           # AI analyst
export DISCORD_WEBHOOK_URL=...         # buy alerts sent to your phone
```

To start over with your real data, run `rm data/ghostsignal.db`.

## 1. Get your Amazon order history

Amazon doesn't offer a one-click export anymore. There are two ways to get it:

**A. Instant: browser exporter.** This runs in your own logged-in browser and sends nothing anywhere.

1. Open https://www.amazon.com/your-orders/orders
2. Open DevTools → Console (`Cmd+Opt+J` / `Ctrl+Shift+J`).
3. Paste the contents of `tools/amazon_orders_scraper.js` and press Enter. Chrome may ask you to type `allow pasting` first.
4. Enter a label such as `me`. It goes through each year (about 1.5 s per page) and downloads `amazon-orders-me.json` and `.csv`. It also copies the ASIN list to your clipboard so you can paste it straight into Stealth Seller.

```bash
gs import-orders ~/Downloads/amazon-orders-me.json --buyer me
```

**B. Official: Amazon "Request Your Data".** This is slower (it can take days) but complete. Go to Amazon → Account → *Request Your Data* → *Your Orders*. Then import the `Retail.OrderHistory.*.csv` file:

```bash
gs import-orders Retail.OrderHistory.1.csv --buyer gf
```

**Friends and family:** send them the script and these steps. They run it on their own account and send you the JSON file. Nobody shares a password. Only product fields are stored (ASIN, title, date, qty, price). Addresses and payment details are dropped on import, and `buyer` is just a label you choose.

## 2. Daily workflow

```bash
gs top                              # ranked opportunities
gs show B00F0FC3OC                  # why it scored what it did
gs links B00F0FC3OC                 # search links for Walmart, Target, Costco, Sam's, CVS…
gs source add B00F0FC3OC costco 10.99 --promo "10-ct box" --in-stock
gs export asins --verdict RESEARCH  # paste into Stealth Seller or Keepa for deeper research
gs status B00NLVM6WK pass           # done with it (it stays in history)
```

The dashboard (`gs serve`) is a simple list: click any product to see the numbers, where to buy it, and to log a store price. **Add** pastes ASINs or uploads your order files; **Scan** rescores everything.

## 3. Automation

`gs run` pulls tracked seller storefronts, refreshes stale products from Keepa, runs AI enrichment on new products, rescores everything and sends alerts. Put it on a schedule:

```cron
# every 6 hours
0 */6 * * *  cd ~/GhostSignal && KEEPA_API_KEY=... DISCORD_WEBHOOK_URL=... gs run --stale-days 1
```

You get an alert when a product **moves up** (PASS→RESEARCH, anything→BUY) or when a BUY's score jumps by 10 or more. Unchanged products don't send anything.

```
🚨 PASS → BUY — BUY 79/100
Pocky Cream Covered Biscuit Sticks, Strawberry (B00F0FC3OC)
Sell $30.89 | Max cost $15.93
Profit $9.72 | ROI 88%
• ~100 sold/month
• Bought 4x by 3 people in your network, 1 repeat
• Source: costco @ $10.99 (10-ct box)
```

## How scoring works (`ghostsignal/scoring.py`)

| Component | Max pts | Signal |
|---|---|---|
| Profit | 22 | $/unit after referral fee, FBA fee and inbound shipping ($10+ gets full marks) |
| ROI | 18 | 100%+ gets full marks |
| Demand | 20 | monthly sold (log scale) or sales rank |
| Competition | 12 | offer count; reduced heavily if Amazon is on the listing |
| Network | 12 | how many people you know bought it, and repeat buyers |
| Availability | 8 | known in-stock source |
| Stability | 8 | Buy Box vs 90-day average (flags price crashes and spikes) |

**Gated status overrides everything.** A product you can't sell is always PASS, and one that needs approval tops out at RESEARCH. Gated status comes from Amazon's Seller API (`gs check-gated`, free) or from you tapping Yes / Needs approval / No in the dashboard. Until it's checked, the AI's "likely gated" guess costs points instead. Hazmat and IP-complaint flags also subtract points. **BUY** needs score ≥ 70, profit ≥ $3 and ROI ≥ 30%. A product with no source cost yet is **RESEARCH**, and the card shows the Max Cost you'd need to hit. Every threshold is in `Config` and can be tuned.

The fee math is an estimate. Before buying in volume, confirm each item in Amazon's Revenue Calculator and check your eligibility (the *Eligibility* link on each card).

## Layout

```
ghostsignal/
  db.py         SQLite schema: products, snapshots, retail_sources, orders, signals, sellers, enrichment
  importers.py  Amazon orders (3 formats) + any product CSV (header aliases)
  scoring.py    fees, profit/ROI, Max Cost, 0–100 score, verdict, reasons
  engine.py     score loop, change detection, alerts (Discord or console)
  keepa.py      Keepa API: product refresh + seller storefront tracking
  enrich.py     Claude product analyst: gating/hazmat/IP risk, replenishable, likely retailers
  sources.py    retailer + marketplace search links
  stores.py     store prices near you: Google Lens + Google Shopping via SerpAPI
  spapi.py      Amazon Seller API: real gated check + real fees
  setup_status.py  the connections checklist (gs setup / Setup page)
  server.py     dashboard API (stdlib)
  web/          dashboard UI
tools/amazon_orders_scraper.js
```

## Roadmap

- [ ] Retailer price checks (official APIs/affiliate feeds where available, e.g. Walmart, Best Buy)
- [ ] Promo/deal feed: clearance, B2G1, circle offers, matched against the watchlist
- [ ] eBay sold-comps via the eBay Browse/Marketplace Insights API
- [ ] Per-store inventory checks
- [ ] Track outcomes (what you bought, what sold, real profit) and feed them back into the weights
- [ ] Postgres/Supabase backend for multi-device use and sharing with a partner

## Ground rules

- Only import order data from people who **chose** to share it with you.
- Use APIs and exports you have rights to (Keepa API, your own Stealth Seller exports). Don't scrape services in ways their terms forbid.
- Data improves decisions but doesn't remove risk. Gating, IP complaints, price crashes and listing floods all still happen. Treat the system as your analyst, not your boss.
