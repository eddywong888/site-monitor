# site-monitor

Outside-in checks for {websites}, run by GitHub Actions and sent to Telegram.

| Workflow | When (MYT) | What you get |
|---|---|---|
| Server down check | every 10 min | 🔴 when a site is down (after one retry), a reminder every hour while it's down, 🟢 when it's back |
| Daily uptime report | 08:07 daily | 24h / 7d uptime, outages, average and p95 response time, SSL days left |
| Weekly broken-link report | Sun 03:13 | Broken internal links (with the pages linking to them), dead sitemap URLs, broken external links + full `.md` list |
| Weekly SEO and speed report | Mon 09:23 | PageSpeed (mobile) scores + LCP/CLS/TBT, robots.txt, sitemap, redirects, SSL, title/description/h1/canonical/noindex on ~28 pages per site + full `.md` list |
| Keep schedules alive | 1st of month | One small commit, so GitHub doesn't pause the schedules after 60 quiet days |

GitHub's scheduler can start runs 5–20 minutes late at busy times.

## What this repo must never contain

It is **public**, so it names no site. The site list lives in the `SITES_JSON` secret; logs say
"site 1", "site 2" and GitHub masks the real names and hosts; the cached state keys sites by a hash.
Never commit site names or URLs, server IPs, SSH hosts, paths, DB details, logins or security findings.
Details go to Telegram only.

## Setup

1. **Telegram bot** — in Telegram, message **@BotFather** → `/newbot` → pick a name → copy the token.
   Then open your new bot and press **Start** (a bot can't message you first).
2. **Your chat ID** — open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser after pressing Start;
   copy `"chat":{"id":…}`.
3. **Repo secrets** — GitHub → this repo → Settings → Secrets and variables → Actions → New repository secret:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `PSI_API_KEY` (needed for the speed scores; Google gives keyless calls no quota) — Google Cloud console → enable *PageSpeed Insights API* →
     Credentials → API key. Restrict the key to the PageSpeed Insights API.
   - `SITES_JSON` — the site list, shaped like `sites.example.json` (`pages` = extra pages to speed-test).
4. **Test** — Actions tab → pick a workflow → *Run workflow* (pick sites by number, never by name).

## Adding or removing a site

Edit your local, git-ignored `sites.json`, then `gh secret set SITES_JSON < sites.json`.

## Local run

```bash
python3 monitor/down.py              # no Telegram secrets → prints to the terminal
MAX_PAGES=40 python3 monitor/links.py 1   # site 1 only
python3 monitor/seo_speed.py 2
```

## Load on the shops

The link crawler fetches one page at a time per site with a 0.7 s pause, at most 2,500 URLs per site,
skipping cart/checkout/account/wp-admin and any URL with `?` — gentle enough for the 1GB servers.
Requests identify as `site-monitor/1.0 (+https://github.com/eddywong888/site-monitor)`.
