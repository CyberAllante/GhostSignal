# Host GhostSignal on Railway

GhostSignal runs as one service with a small disk (volume) for the database. You upload order files in the browser, so nothing needs to run on your Mac.

## 1. Create the service

1. Railway → **New Project → Deploy from GitHub repo → CyberAllante/GhostSignal**.
2. Pick the branch `claude/product-tracking-resale-db-gc2kr2` (Settings → Source → Branch), or merge it to `main` and use that.
3. Railway finds the `Dockerfile` and `railway.json` on its own.

## 2. Add the volume (this is what keeps your data)

Without it, every redeploy wipes the database.

Service → **Settings → Volumes → New Volume → Mount path: `/data`**.

## 3. Set the variables (Service → Variables)

| Variable | Value | Needed? |
|---|---|---|
| `GHOSTSIGNAL_PASSWORD` | a long password you pick | **Yes.** The app refuses to start without it, because your order data is on a public URL. |
| `GHOSTSIGNAL_AUTO_RUN_HOURS` | `24` (daily) or `168` (weekly) | Recommended. Refreshes prices and scores on its own. |
| `GHOSTSIGNAL_UPLOAD_TOKEN` | any random string | Recommended. Lets the order exporter send orders straight to your app (see MCP.md). |
| `KEEPA_API_KEY` | from keepa.com | When you have it |
| `SERPAPI_KEY` | from serpapi.com | When you have it |
| `SPAPI_CLIENT_ID`, `SPAPI_CLIENT_SECRET`, `SPAPI_REFRESH_TOKEN`, `SPAPI_SELLER_ID` | from Seller Central | When you have them |
| `ANTHROPIC_API_KEY` | from console.anthropic.com | Optional (AI risk flags) |
| `DISCORD_WEBHOOK_URL` | a Discord webhook | Optional (buy alerts) |

You don't set `PORT` or `GHOSTSIGNAL_DB`; they're handled for you.

## 4. Open it

Service → **Settings → Networking → Generate Domain**. Open the link. Your browser asks for a login: **any username, your `GHOSTSIGNAL_PASSWORD`** as the password.

## 5. Load the data

1. Dashboard → **Import orders**. Follow the two steps (copy the exporter script, run it on amazon.com for each person, upload the files).
2. Add the keys above as you get them. After each import, and on the schedule, it looks up prices, fees and gated status for whatever keys are connected.
3. If you see sample data, click **Clear sample data**. (Railway starts empty, so you only see samples if you loaded them.)

## Connect Claude

Once it's running, follow **MCP.md** so Claude can run it for you.

## Notes

- Redeploys keep your data as long as the volume is attached.
- Railway's Hobby plan covers this easily; it's one small process and a SQLite file.
- To back up: Dashboard → Products → **Export** downloads a CSV of everything.
