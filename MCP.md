# Let Claude run GhostSignal (MCP)

GhostSignal has an MCP server built in. Once it's connected, you talk to Claude and Claude does the work: imports orders, looks things up, fills gaps, and tells you what's worth buying. You don't click through the dashboard.

It lives on your Railway app at `https://YOUR-APP.up.railway.app/mcp` and uses your `GHOSTSIGNAL_PASSWORD` as the key.

## Connect it

**Claude Code** (in Terminal):

```bash
claude mcp add --transport http ghostsignal https://YOUR-APP.up.railway.app/mcp \
  --header "Authorization: Bearer YOUR_PASSWORD"
```

**Claude Desktop** (needs Node installed). Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ghostsignal": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://YOUR-APP.up.railway.app/mcp",
               "--header", "Authorization: Bearer YOUR_PASSWORD"]
    }
  }
}
```

Restart the app. Then try: **"What does GhostSignal say? Anything worth buying?"**

## What you can ask

| You say | It does |
|---|---|
| "What should I buy?" | `picks`: best products, one line each |
| "Anything new this week?" | `what_changed`: products that got better, including old passes that came back |
| "Why is the Pocky a buy?" | `product`: numbers, stores, history, notes |
| "What can't you judge yet?" | `todo`: products missing data, and what's missing |
| "Here's my girlfriend's order file" | `import_orders`, then it looks everything up |
| "Is this gated for me?" / "It sells for $40 on eBay" | `set_gated` / `set_sell_price`, rescored right away |
| "I bought 6 of these at Costco for $10.99" | `log_purchase` |
| "Track this seller" | `track_seller` |
| "Refresh everything" | `run_now` |

## How "always working" works

Two layers:

1. **The service itself** (no AI needed). With `GHOSTSIGNAL_AUTO_RUN_HOURS=24` it refreshes Keepa data, store prices and gating, rescores everything and sends Discord alerts on its own. It's built for a huge list:
   - Good products are rechecked daily, "research" ones every 3 days.
   - Products that look dead are rechecked every 2-4 weeks, **never dropped**. If an old PASS turns good, the alert says `REVIVED after 200d`.
   - A history row is only saved when something changes, so the database stays small.
   - Keepa lookups are capped per run (best products first) so a big list doesn't burn your tokens.
2. **Claude, when you talk to it.** It reads the picks, fills in what the automation can't (pack sizes, gating, eBay and Facebook prices), saves notes so research isn't repeated, and asks you a short question only when it needs one.

## Sending order files without any file handling

On your Railway app, set `GHOSTSIGNAL_UPLOAD_TOKEN` (any random string). Then **Import orders → Copy the exporter script** gives a script with your app's address built in. Whoever runs it on amazon.com sends their orders straight to GhostSignal. That token can only add orders, nothing else. If Amazon's page blocks the upload, the script falls back to downloading a file.

## Not tested yet

The server was exercised end to end with a test client (connect, list tools, call them, bad input, wrong password). I haven't connected a real Claude client to a live Railway deploy, so expect a small fix or two on the first try.
