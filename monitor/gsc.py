"""Google Search Console summary: last 28 days vs the 28 before, top queries/pages, movers, sitemaps.

Needs an OAuth access token with the webmasters.readonly scope in GSC_TOKEN (the workflow gets one
keylessly via google-github-actions/auth). The service account must be a user on each property.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

from common import esc, now_myt, safe_main, send, send_file, sites

API = "https://www.googleapis.com/webmasters/v3"
LAG_DAYS = 3  # Search Console data settles after ~2–3 days
DAYS = 28


def call(path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body else None, headers={
        "Authorization": f"Bearer {os.environ['GSC_TOKEN']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def find_property(url, props):
    """Prefer the domain property (covers http/https/www), else a URL-prefix one for the same host."""
    host = urllib.parse.urlsplit(url).hostname.removeprefix("www.")
    for p in (f"sc-domain:{host}", f"https://{host}/", f"https://www.{host}/", f"http://{host}/"):
        if p in props:
            return p
    return None


def query(prop, start, end, dims=(), limit=1000):
    body = {"startDate": str(start), "endDate": str(end), "dimensions": list(dims), "rowLimit": limit}
    rows = call(f"/sites/{urllib.parse.quote(prop, safe='')}/searchAnalytics/query", body).get("rows", [])
    return rows


def totals(rows):
    r = rows[0] if rows else {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}
    return r["clicks"], r["impressions"], r["ctr"], r["position"]


def delta(change, unit):
    """▲ always means better, ▼ worse."""
    arrow = "▲" if change > 0 else "▼" if change < 0 else "•"
    return f" {arrow}{abs(change):.1f}{unit}".replace(".0%", "%")


def pct_change(now, before):
    return delta((now - before) / before * 100, "%") if before else ""


def summary(site, prop):
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=DAYS - 1)
    p_end, p_start = start - timedelta(days=1), start - timedelta(days=DAYS)
    c, i, ctr, pos = totals(query(prop, start, end))
    pc, pi, pctr, ppos = totals(query(prop, p_start, p_end))
    q_now = {r["keys"][0]: r for r in query(prop, start, end, ["query"])}
    q_before = {r["keys"][0]: r for r in query(prop, p_start, p_end, ["query"])}
    pages = query(prop, start, end, ["page"], 25)
    devices = query(prop, start, end, ["device"])
    maps = call(f"/sites/{urllib.parse.quote(prop, safe='')}/sitemaps").get("sitemap", [])

    moves = sorted(((q, q_now.get(q, {}).get("clicks", 0) - q_before.get(q, {}).get("clicks", 0))
                    for q in set(q_now) | set(q_before)), key=lambda x: x[1])
    losers = [m for m in moves[:3] if m[1] < 0]
    winners = [m for m in reversed(moves[-3:]) if m[1] > 0]
    top = sorted(q_now.values(), key=lambda r: -r["clicks"])

    msg = [f"🔎 <b>Search Console — {esc(site['name'])}</b>",
           f"<i>{start:%d %b} – {end:%d %b} vs previous 28 days</i>",
           f"Clicks <b>{c:,.0f}</b>{pct_change(c, pc)} · Impressions <b>{i:,.0f}</b>{pct_change(i, pi)}",
           f"CTR <b>{ctr * 100:.1f}%</b>{delta((ctr - pctr) * 100, ' pt') if pi else ''} "
           f"· Avg position <b>{pos:.1f}</b>{delta(ppos - pos, '') if ppos else ''}",
           "<i>▲ better · ▼ worse than the previous 28 days</i>",
           "Devices: " + " · ".join(f"{r['keys'][0].title()} {r['clicks']:,.0f}" for r in devices)]
    msg.append("\n<b>Top queries</b> (clicks · position)")
    msg += [f"{n}. {esc(r['keys'][0])} — {r['clicks']:,.0f} · {r['position']:.1f}" for n, r in enumerate(top[:8], 1)]
    if winners or losers:
        msg.append("\n<b>Movers</b> (clicks vs previous 28 days)")
        msg += [f"▲ {esc(q)} +{d:,.0f}" for q, d in winners] + [f"▼ {esc(q)} {d:,.0f}" for q, d in losers]
    msg.append("\n<b>Top pages</b> (clicks)")
    msg += [f"• {esc(urllib.parse.urlsplit(r['keys'][0]).path or '/')} — {r['clicks']:,.0f}" for r in pages[:5]]
    msg.append("\n<b>Sitemaps</b>")
    for m in maps:
        sub = sum(int(x.get("submitted", 0)) for x in m.get("contents", []))
        bad = int(m.get("errors", 0)) + int(m.get("warnings", 0))
        flag = "⚠️" if bad or m.get("isPending") else "✅"
        msg.append(f"{flag} {esc(urllib.parse.urlsplit(m['path']).path)} — {sub} URLs, read {m.get('lastDownloaded', '?')[:10]}"
                   + (f", {m.get('errors', 0)} errors / {m.get('warnings', 0)} warnings" if bad else ""))
    if not maps:
        msg.append("⚠️ no sitemap submitted")

    md = [f"# Search Console — {site['name']} ({prop})", f"{start} – {end}\n", "## Top 50 queries",
          "| Query | Clicks | Impressions | CTR | Position |", "|---|---|---|---|---|"]
    md += [f"| {r['keys'][0]} | {r['clicks']:.0f} | {r['impressions']:.0f} | {r['ctr'] * 100:.1f}% | {r['position']:.1f} |"
           for r in top[:50]]
    md += ["\n## Top 25 pages", "| Page | Clicks | Impressions | Position |", "|---|---|---|---|"]
    md += [f"| {r['keys'][0]} | {r['clicks']:.0f} | {r['impressions']:.0f} | {r['position']:.1f} |" for r in pages]
    return "\n".join(msg), "\n".join(md)


def main():
    props = {s["siteUrl"] for s in call("/sites").get("siteEntry", [])}
    picked = sites(sys.argv[1:])
    if not picked:
        raise SystemExit("!! no site matches the input")
    for site in picked:
        prop = find_property(site["url"], props)
        if not prop:
            send(f"⚠️ Search Console: no access to {esc(site['name'])}. Add the monitor's service account as a "
                 f"Restricted user in Search Console → Settings → Users and permissions.")
            print(f"{site['label']}: no access")
            continue
        msg, md = summary(site, prop)
        send(msg + f"\n\n<i>{now_myt()}</i>")
        send_file(f"gsc-{site['label'].replace(' ', '')}-{time.strftime('%Y-%m-%d')}.md", md, "Top 50 queries + 25 pages")
        print(f"{site['label']}: sent")


if __name__ == "__main__":
    safe_main(main)
