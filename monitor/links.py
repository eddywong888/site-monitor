"""Weekly broken-link finder.

Crawls each site's own pages (homepage + sitemap, then links found), one request at a time with a
pause — the shops run on 1GB servers. External links are checked once each, in parallel.
"""
import os
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from common import (UNVERIFIABLE, esc, fetch, is_broken, now_myt, parse, safe_main, same_site, send, send_file,
                    sitemap_urls, sites)

MAX_PAGES = int(os.environ.get("MAX_PAGES", 2500))
MAX_EXTERNAL = 400
PAUSE = 0.7
SKIP = re.compile(r"/(wp-admin|wp-login|wp-json|cart|checkout|my-account|feed|xmlrpc|cdn-cgi|comments)(/|\.php|$)"
                  r"|/(add-to-cart|wishlist)|/page/\d+", re.I)
NOT_HTML = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|ico)$", re.I)


def internal_ok(url, base):
    p = urllib.parse.urlsplit(url)
    return same_site(url, base) and p.scheme in ("http", "https") and not p.query and not SKIP.search(p.path)


def crawl(site):
    base = site["url"]
    queue = [base + "/"] + sitemap_urls(base, limit=MAX_PAGES)
    in_sitemap = set(queue[1:])
    seen, status, refs, external = set(queue), {}, defaultdict(set), defaultdict(set)
    fetched = 0
    while queue and fetched < MAX_PAGES:
        url = queue.pop(0)
        r = fetch(url, method="HEAD" if NOT_HTML.search(url) else "GET")
        if r.status == 429:
            time.sleep(15)
            r = fetch(url, method="HEAD" if NOT_HTML.search(url) else "GET")
        fetched += 1
        status[url] = (r.status, r.error)
        time.sleep(PAUSE)
        if r.status != 200 or not same_site(r.url, base) or NOT_HTML.search(url):
            continue
        for link in parse(r).links:
            if same_site(link, base):
                refs[link].add(url)
                if link not in seen and internal_ok(link, base):
                    seen.add(link)
                    queue.append(link)
            elif link.startswith("http"):
                external[link].add(url)
    broken = {u: s or e for u, (s, e) in status.items() if is_broken(s, e)}
    unverified = sum(1 for s, e in status.values() if s and not is_broken(s, e) and s >= 400)
    return {"site": site, "pages": fetched, "left": len(queue), "broken": broken, "unverified": unverified,
            "refs": refs, "in_sitemap": in_sitemap, "external": external}


def check_external(url):
    r = fetch(url, method="HEAD", timeout=20)
    if r.status in (0, 400, 403, 404, 405, 501):  # many servers mishandle HEAD
        r = fetch(url, timeout=20, max_bytes=50_000)
    return url, r.status, r.error


def main():
    picked = sites(sys.argv[1:])
    if not picked:
        raise SystemExit("!! no site matches the input")
    with ThreadPoolExecutor(len(picked)) as pool:  # one thread per site = one request at a time per server
        results = list(pool.map(crawl, picked))
    ext_refs = defaultdict(set)
    for res in results:
        for u, pages in list(res["external"].items()):
            ext_refs[u] |= {(res["site"]["name"], p) for p in pages}
    with ThreadPoolExecutor(8) as pool:
        ext = list(pool.map(check_external, list(ext_refs)[:MAX_EXTERNAL]))
    ext_broken = [(u, s, e) for u, s, e in ext if is_broken(s, e)]
    ext_unver = sum(1 for u, s, e in ext if not is_broken(s, e) and (s == 0 or s >= 400))

    md = [f"# Broken links — {now_myt()}\n"]
    summary = [f"🔗 <b>Weekly broken-link report</b> — {now_myt()}"]
    for res in results:
        name, b = res["site"]["name"], res["broken"]
        mine = [x for x in ext_broken if any(s == name for s, _ in ext_refs[x[0]])]
        flag = "✅" if not b and not mine else "⚠️"
        cap = f" (stopped at {MAX_PAGES}, {res['left']} not checked)" if res["left"] else ""
        if res["unverified"]:
            cap += f" · {res['unverified']} refused the checker (403/429) — not counted"
        summary.append(f"\n{flag} <b>{name}</b>: {len(b)} broken internal, {len(mine)} broken external · "
                       f"{res['pages']} URLs checked{cap}")
        md.append(f"\n## {name} — {res['site']['url']}\n\n{res['pages']} URLs checked{cap}.\n")
        md.append(f"### Broken internal links ({len(b)})\n")
        for u, s in sorted(b.items(), key=lambda x: str(x[1])):
            where = sorted(res["refs"].get(u, []))[:5]
            tag = " · listed in sitemap" if u in res["in_sitemap"] else ""
            md.append(f"- **{s}** {u}{tag}" + ("\n  - linked from: " + ", ".join(where) if where else ""))
        for u, s in list(b.items())[:5]:
            summary.append(f"  • {esc(s)} {esc(u)}")
        md.append(f"\n### Broken external links ({len(mine)})\n")
        for u, s, e in mine:
            where = sorted(p for site_name, p in ext_refs[u] if site_name == name)[:5]
            md.append(f"- **{s or esc(e)}** {u}\n  - linked from: " + ", ".join(where))
    summary.append(f"\n{ext_unver} external links refused bots or timed out — not counted as broken. Full list attached.")
    md.append(f"\n---\n{len(ext)} external links checked; {ext_unver} refused automated checks or timed out and were skipped.\n")
    for res in results:
        print(f"{res['site']['label']}: {res['pages']} checked, {len(res['broken'])} broken")
    send("\n".join(summary))
    send_file(f"broken-links-{time.strftime('%Y-%m-%d')}.md", "\n".join(md), "Broken links — full list")


if __name__ == "__main__":
    safe_main(main)
