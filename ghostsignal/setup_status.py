"""What's connected and what's left to do. Used by `gs setup` and the dashboard."""

from __future__ import annotations

import os
import shutil
import subprocess

from . import db, spapi, stores


def checklist(conn) -> list[dict]:
    n = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    try:
        import anthropic  # noqa: F401
        has_sdk = True
    except ImportError:
        has_sdk = False
    cron = ""
    if shutil.which("crontab"):
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    return [
        {"key": "products", "label": "Products in your database", "cost": "",
         "done": n("SELECT COUNT(*) FROM products WHERE origin NOT LIKE '%demo%'") > 0,
         "what": "Paste ASINs, import a Stealth Seller export, or import orders.",
         "how": "Products → Add"},
        {"key": "orders", "label": "Your Amazon orders imported", "cost": "",
         "done": n("SELECT COUNT(*) FROM orders WHERE source_file != 'demo'") > 0,
         "what": "Real purchase data from you and people you know.",
         "how": "Run tools/amazon_orders_scraper.js on amazon.com, then Products → Add → upload"},
        {"key": "location", "label": "Your area set", "cost": "",
         "done": bool(db.get_setting(conn, "location")),
         "what": "So store prices match the stores near you.",
         "how": "Setup → Your area"},
        {"key": "spapi", "label": "Amazon Seller API", "cost": "Free",
         "done": spapi.configured(),
         "what": "Real gated check for YOUR account + Amazon's real fees.",
         "how": "Seller Central → Apps and Services → Develop Apps (see SETUP.md), add SPAPI_* to .env"},
        {"key": "keepa", "label": "Keepa API", "cost": "Paid monthly",
         "done": bool(os.environ.get("KEEPA_API_KEY")),
         "what": "Seller tracking, sales/month, rank, price history — the data Stealth Seller shows.",
         "how": "keepa.com → API → subscribe, add KEEPA_API_KEY to .env"},
        {"key": "serpapi", "label": "Store prices (SerpAPI)", "cost": "Free tier, then paid",
         "done": stores.configured(),
         "what": "Walmart/Target/Costco/Sam's prices near you, from Google Shopping.",
         "how": "serpapi.com → sign up → add SERPAPI_KEY to .env (same as Telly)"},
        {"key": "claude", "label": "Claude AI flags", "cost": "Pennies per batch",
         "done": bool(os.environ.get("ANTHROPIC_API_KEY")) and has_sdk,
         "what": "Hazmat, IP-complaint brands, likely stores.",
         "how": "console.anthropic.com → API key → ANTHROPIC_API_KEY in .env, then pip install anthropic"},
        {"key": "discord", "label": "Phone alerts (Discord)", "cost": "Free",
         "done": bool(os.environ.get("DISCORD_WEBHOOK_URL")),
         "what": "Get pinged when something turns into a Buy.",
         "how": "Discord channel → Edit → Integrations → Webhooks → DISCORD_WEBHOOK_URL in .env"},
        {"key": "cron", "label": "Automatic scans", "cost": "",
         "done": "gs run" in cron,
         "what": "Runs everything on a schedule so you don't have to.",
         "how": "crontab -e  →  0 9 * * * cd ~/ghostsignal && .venv/bin/gs run"},
    ]
