# Setup on your Mac (step by step)

Everything happens in the **Terminal** app. Copy each block, paste it, and press Enter.

## 1. Make sure Python is installed

```bash
python3 --version
```

If you see `Python 3.10` or newer, go to step 2. If a box pops up asking to install "command line developer tools", click **Install**, wait for it to finish, then run the command again. If the version is older than 3.10, install a newer one from https://www.python.org/downloads/ and run the command again.

## 2. Download GhostSignal

```bash
cd ~
git clone -b claude/product-tracking-resale-db-gc2kr2 https://github.com/CyberAllante/GhostSignal.git ghostsignal
cd ghostsignal
```

If it asks for a GitHub password, the repo is private and you need a token. The easier route is GitHub's website: open the repo, switch to the `claude/product-tracking-resale-db-gc2kr2` branch, click **Code → Download ZIP**, unzip the folder, rename it to `ghostsignal`, move it to your home folder, and run `cd ~/ghostsignal`.

## 3. Run it (nothing to install)

GhostSignal doesn't need any packages, so skip `pip` and the `.venv`. If you already tried, clear the broken one first:

```bash
rm -rf .venv
```

Then:

```bash
./gs demo      # optional: loads sample products so the page isn't empty
./gs serve
```

Leave that Terminal window open and go to **http://127.0.0.1:8787** in your browser. If the page says "can't be reached", the server isn't running; run `./gs serve` again and keep the window open. Press `Ctrl+C` to stop it.

**Every time you come back:**

```bash
cd ~/ghostsignal && ./gs serve
```

**To get the newest version:**

```bash
cd ~/ghostsignal && git pull
```

## 4. See what's connected

```bash
./gs setup
```

This prints a checklist of what's done and what's left. Nothing is required except steps 1–3.

## 5. Add your keys (when you're ready)

```bash
cp .env.example .env
open -e .env
```

Fill in whatever you have, save, and you're done. `./gs` reads this file on its own. Restart `./gs serve` after changing it.

To start fresh without the sample products, run `rm data/ghostsignal.db`.

---

## Getting the Amazon Seller API keys (free; this is what checks gated status)

You need a **Professional** seller account ($39.99/mo, the one you'd sell on anyway).

1. Seller Central → **Apps and Services → Develop Apps**.
2. Register as a developer. Choose **private developer** (an app only for your own account). Amazon may take a few days to approve it.
3. Click **Add new app client**. For API type choose **SP API**, and select the roles **Product Listing** and **Pricing**.
4. In the app list, click **View** next to the credentials to get the **Client ID** and **Client Secret**.
5. Click **Authorize** next to the app. That gives you the **Refresh Token**.
6. Your **Seller ID** (Merchant Token) is in Seller Central → Settings → **Account Info → Your Merchant Token**.

Put all four in `.env`, then run:

```bash
./gs check-gated   # ungated / needs approval / can't sell, for YOUR account
./gs fees          # Amazon's real fees
./gs market        # price, Buy Box, offers, sales rank, title/brand/image for stale products (free Keepa stand-in)
./gs market --all  # everything, right now
```

With no Keepa key set, `gs run` and the daily auto-run use the Seller API for market data automatically. It does not give price history or sales per month; those need Keepa.
On Railway, add the four `SPAPI_*` values as service variables.
