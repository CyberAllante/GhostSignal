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

## 3. Install it (one time)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

On a Mac, `pip` alone doesn't exist until you run `source .venv/bin/activate`, which turns on the project's own Python.

## 4. Run it

```bash
gs demo      # optional: loads 5 sample products so the page isn't empty
gs serve
```

Open **http://127.0.0.1:8787** in your browser. Press `Ctrl+C` in Terminal to stop it.

**Every time you come back** (new Terminal window):

```bash
cd ~/ghostsignal && source .venv/bin/activate && gs serve
```

## 5. See what's connected

```bash
gs setup
```

This prints a checklist of what's done and what's left. Nothing is required except steps 1–4.

## 6. Add your keys (when you're ready)

```bash
cp .env.example .env
open -e .env
```

Fill in whatever you have, save, and you're done. `gs` reads this file on its own.

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
gs check-gated     # ungated / needs approval / can't sell, for YOUR account
gs fees            # Amazon's real fees
```
