"""Weekly SEO + speed check.

Speed: Google PageSpeed Insights (mobile) for the homepage + each site's key pages.
SEO: robots.txt, sitemap, HTTPS/www redirects, and on-page basics for a sample of sitemap pages.
"""
import json
import os
import random
import sys
import time
import urllib.parse
from collections import Counter

from common import (cert_days_left, esc, fetch, now_myt, parse, robots_sitemaps, safe_main, send, send_file, sitemap_urls,
                    sites, text)

PSI = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
SAMPLE = 25


def pagespeed(url):
    q = [("url", url), ("strategy", "mobile")] + [("category", c) for c in
                                                  ("performance", "seo", "accessibility", "best-practices")]
    if not os.environ.get("PSI_API_KEY"):  # Google gives keyless callers no quota (HTTP 429)
        return {"error": "speed skipped — add the PSI_API_KEY secret"}
    q.append(("key", os.environ["PSI_API_KEY"]))
    for attempt in range(2):
        r = fetch(PSI + "?" + urllib.parse.urlencode(q), timeout=90, max_bytes=50_000_000)
        if r.status == 200:
            break
        time.sleep(20)
    else:
        return {"error": f"PageSpeed API HTTP {r.status or r.error}"}
    d = json.loads(r.body)
    lh, cats = d["lighthouseResult"], d["lighthouseResult"]["categories"]
    audit = lambda k: lh["audits"].get(k, {}).get("displayValue", "–")
    field = d.get("loadingExperience", {}).get("overall_category")  # real-user Chrome data, if Google has it
    return {"perf": round(cats["performance"]["score"] * 100), "seo": round(cats["seo"]["score"] * 100),
            "a11y": round(cats["accessibility"]["score"] * 100),
            "bp": round(cats["best-practices"]["score"] * 100),
            "lcp": audit("largest-contentful-paint"), "cls": audit("cumulative-layout-shift"),
            "tbt": audit("total-blocking-time"), "field": field}


def page_issues(url):
    r = fetch(url)
    if r.status != 200:
        return r, None, [f"HTTP {r.status or r.error}"]
    p, issues = parse(r), []
    robots = (p.meta.get("robots", "") + " " + r.headers.get("X-Robots-Tag", "")).lower()
    if "noindex" in robots:
        issues.append("❗ noindex")
    if not p.title:
        issues.append("no <title>")
    elif not 15 <= len(p.title) <= 65:
        issues.append(f"title {len(p.title)} chars")
    desc = p.meta.get("description", "").strip()
    if not desc:
        issues.append("no meta description")
    elif not 50 <= len(desc) <= 165:
        issues.append(f"description {len(desc)} chars")
    if len(p.h1) != 1:
        issues.append(f"{len(p.h1)} × <h1>")
    if not p.canonical:
        issues.append("no canonical")
    elif urllib.parse.urljoin(r.url, p.canonical).rstrip("/") != r.url.rstrip("/"):
        issues.append(f"canonical → {p.canonical}")
    return r, p, issues


def blocks_everything(robots_txt):
    """True if the 'User-agent: *' group has a bare 'Disallow: /'."""
    agents, in_star = [], False
    for raw in robots_txt.lower().splitlines():
        line = raw.split("#")[0].strip()
        if line.startswith("user-agent:"):
            if not agents:
                in_star = False
            agents.append(line.split(":", 1)[1].strip())
            in_star = in_star or agents[-1] == "*"
            continue
        agents = []
        if in_star and line.replace(" ", "") == "disallow:/":
            return True
    return False


def site_checks(site):
    base, host = site["url"], urllib.parse.urlsplit(site["url"]).hostname
    out = []
    robots = fetch(base + "/robots.txt")
    if robots.status != 200:
        out.append(f"robots.txt HTTP {robots.status}")
    elif blocks_everything(text(robots)):
        out.append("❗ robots.txt blocks all crawlers ('Disallow: /' for *)")
    listed = robots_sitemaps(robots)
    if not listed:
        out.append("robots.txt names no Sitemap")
    bad = {}
    urls = sitemap_urls(base, listed=listed, bad=bad)
    for m in listed:
        if m in bad:
            out.append(f"❗ robots.txt sitemap {m} → HTTP {bad[m]} (Google can't read it)")
    out.append(f"sitemap: {len(urls)} URLs" if urls else "❗ no sitemap found")
    for src in (f"http://{host}/", f"https://www.{host.removeprefix('www.')}/" if not host.startswith("www.")
                else f"https://{host[4:]}/"):
        r = fetch(src, timeout=20)
        if r.status != 200 or urllib.parse.urlsplit(r.url).hostname != host or not r.url.startswith("https://"):
            out.append(f"{src} → {r.url} (HTTP {r.status or r.error})")
    days = cert_days_left(base)
    if days is None or days < 21:
        out.append(f"❗ SSL certificate: {'unreadable' if days is None else f'{days} days left'}")
    return out, urls


def main():
    lines = [f"🔎 <b>Weekly SEO &amp; speed report</b> — {now_myt()}"]
    md = [f"# SEO & speed — {now_myt()}\n"]
    for site in sites(sys.argv[1:]):
        base, name = site["url"], site["name"]
        lines.append(f"\n<b>{name}</b> — {esc(base)}")
        md.append(f"\n## {name} — {base}\n")
        checks, urls = site_checks(site)
        md.append("### Site\n" + "\n".join(f"- {c}" for c in checks))
        lines += [f"  • {esc(c)}" for c in checks if c.startswith("❗") or "→" in c]

        md.append("\n### Speed (PageSpeed Insights, mobile)\n\n| Page | Perf | SEO | A11y | Best pr. | LCP | CLS | TBT | Real users |\n|---|---|---|---|---|---|---|---|---|")
        for path in ["/"] + site.get("pages", []):
            s = pagespeed(base + path)
            if "error" in s:
                md.append(f"| {path} | {s['error']} |||||||")
                lines.append(f"  ⚡ {esc(path)}: {esc(s['error'])}")
                continue
            dot = "🟢" if s["perf"] >= 90 else "🟠" if s["perf"] >= 50 else "🔴"
            md.append(f"| {path} | {s['perf']} | {s['seo']} | {s['a11y']} | {s['bp']} | {s['lcp']} | {s['cls']} | "
                      f"{s['tbt']} | {s['field'] or 'not enough data'} |")
            lines.append(f"  ⚡ {esc(path)} {dot} speed {s['perf']} · SEO {s['seo']} · LCP {esc(s['lcp'])}")

        sample = [base + "/"] + [base + p for p in site.get("pages", [])]
        rest = [u for u in urls if u not in sample]
        sample += random.Random(time.strftime("%Y%W")).sample(rest, min(SAMPLE, len(rest)))
        md.append(f"\n### On-page SEO ({len(sample)} pages: homepage, key pages, random sitemap sample)\n")
        titles, problems, noindex = Counter(), 0, 0
        for u in sample:
            r, p, issues = page_issues(u)
            if p and p.title:
                titles[p.title] += 1
            noindex += "❗ noindex" in issues
            if issues:
                problems += 1
                md.append(f"- {u}: " + "; ".join(issues))
            time.sleep(0.7)
        dups = [t for t, n in titles.items() if n > 1]
        for t in dups:
            md.append(f"- duplicate title on {titles[t]} pages: “{t}”")
        lines.append(f"  📝 {problems}/{len(sample)} sampled pages with SEO issues"
                     + (f" · ❗ {noindex} noindex" if noindex else "") + (f" · {len(dups)} duplicate titles" if dups else ""))
        print(f"{site['label']}: done")
    lines.append("\nFull details attached.")
    send("\n".join(lines))
    send_file(f"seo-speed-{time.strftime('%Y-%m-%d')}.md", "\n".join(md), "SEO &amp; speed — full details")


if __name__ == "__main__":
    safe_main(main)
