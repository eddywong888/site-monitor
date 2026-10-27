"""Shared helpers: HTTP fetch, HTML parsing, sitemaps, SSL expiry, Telegram.

Standard library only, so the workflows need no pip install.
"""
import gzip
import hashlib
import html
import json
import os
import re
import socket
import ssl
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import namedtuple
from html.parser import HTMLParser

UA = "site-monitor/1.0 (+https://github.com/eddywong888/site-monitor)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_CI = bool(os.environ.get("CI"))

Resp = namedtuple("Resp", "status url body secs error headers")

# Try IPv4 addresses first: urllib has no happy-eyeballs, so a slow IPv6 path skews response times.
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: sorted(_getaddrinfo(*a, **k), key=lambda ai: ai[0] != socket.AF_INET)


def sites(only=None):
    """Sites to check, from the SITES_JSON secret (CI) or the git-ignored sites.json (local).

    The repo is public, so site names and URLs never live in it. Each site gets a neutral
    `label` ("site 1") for log lines and a hashed `id` for the cached state; in CI the real
    names and hosts are also registered as log masks. `only` picks sites by number or name.
    """
    raw = os.environ.get("SITES_JSON")
    if not raw:
        with open(os.path.join(ROOT, "sites.json")) as f:
            raw = f.read()
    all_sites = json.loads(raw)
    for i, s in enumerate(all_sites, 1):
        s["url"] = s["url"].rstrip("/")
        s["label"] = f"site {i}"
        s["id"] = hashlib.sha256(s["url"].encode()).hexdigest()[:12]
        if IN_CI:
            host = urllib.parse.urlsplit(s["url"]).hostname
            for secret in {s["name"], s["url"], host, host.removeprefix("www."), host.split(".")[0]}:
                print(f"::add-mask::{secret}")
    return [s for i, s in enumerate(all_sites, 1) if not only or s["name"] in only or str(i) in only]


def safe_main(main):
    """Run a check; on a crash, log only the error type (public logs) and send the detail to Telegram."""
    try:
        main()
    except TelegramError as e:
        print(f"!! {e}")  # can't report a Telegram failure through Telegram
        raise SystemExit(1) from None
    except Exception as e:  # noqa: BLE001
        print(f"!! {type(e).__name__} — details sent to Telegram")
        try:
            send(f"⚠️ <b>Monitor script failed</b> ({esc(os.environ.get('GITHUB_WORKFLOW', 'local'))})\n"
                 f"<pre>{esc(traceback.format_exc()[-3000:])}</pre>")
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(1) from None


def fetch(url, method="GET", timeout=25, max_bytes=4_000_000):
    """Fetch a URL, following redirects. status 0 = no HTTP answer (DNS, TLS, timeout)."""
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(max_bytes) if method != "HEAD" else b""
            return Resp(r.status, r.geturl(), body, time.monotonic() - t, None, dict(r.headers))
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.geturl() or url, b"", time.monotonic() - t, None, dict(e.headers or {}))
    except Exception as e:  # noqa: BLE001 — any failure is a check result, not a crash
        return Resp(0, url, b"", time.monotonic() - t, f"{type(e).__name__}: {e}"[:140], {})


# Answers that say "can't tell from here" rather than "this link is dead": logins, bot walls, rate limits.
UNVERIFIABLE = {401, 403, 405, 429, 999}


def is_broken(status, error=None):
    """A link is broken on a definite failure (404, 410, 5xx, DNS/TLS error), not on a bot wall or timeout."""
    if status in UNVERIFIABLE:
        return False
    if status == 0:
        return "timed out" not in (error or "").lower()
    return status >= 400


def text(resp):
    return resp.body.decode("utf-8", "replace")


def same_site(a, b):
    host = lambda u: (urllib.parse.urlsplit(u).hostname or "").removeprefix("www.")
    return host(a) == host(b)


class Page(HTMLParser):
    """Collects what the checks need from one HTML page."""

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links, self.h1, self.meta = [], [], {}
        self.title, self.canonical, self.lang = None, None, None
        self._in = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            href = a["href"].strip()
            if not href.startswith(("mailto:", "tel:", "javascript:", "#", "data:", "whatsapp:")):
                self.links.append(urllib.parse.urldefrag(urllib.parse.urljoin(self.base, href))[0])
        elif tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta" and a.get("name"):
            self.meta[a["name"].lower()] = a.get("content") or ""
        elif tag == "link" and "canonical" in (a.get("rel") or "").lower().split():
            self.canonical = a.get("href")
        elif tag in ("title", "h1") and self._in is None:
            self._in = tag
            if tag == "title":
                self.title = ""
            else:
                self.h1.append("")

    def handle_endtag(self, tag):
        if tag == self._in:
            self._in = None

    def handle_data(self, data):
        if self._in == "title" and self.title is not None:
            self.title += data
        elif self._in == "h1":
            self.h1[-1] += data


def parse(resp):
    p = Page(resp.url)
    try:
        p.feed(text(resp))
    except Exception:  # noqa: BLE001 — broken markup still gives partial results
        pass
    p.title = " ".join((p.title or "").split()) or None
    p.h1 = [" ".join(h.split()) for h in p.h1]
    return p


def robots_sitemaps(robots):
    """Sitemap URLs named in a fetched robots.txt response."""
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text(robots)) if robots.status == 200 else []


def sitemap_urls(base, limit=5000, listed=None, bad=None):
    """Page URLs from the site's sitemap(s): robots.txt Sitemap lines first, then common names.

    `listed` skips re-reading robots.txt; `bad` collects {sitemap URL: status} for ones that failed.
    """
    if listed is None:
        listed = robots_sitemaps(fetch(base + "/robots.txt"))
    pages = _read_sitemaps(listed, limit, bad)
    return pages or _read_sitemaps([base + "/sitemap_index.xml", base + "/sitemap.xml", base + "/wp-sitemap.xml"], limit)


def _read_sitemaps(maps, limit, bad=None):
    maps, seen, pages = list(maps), set(), []
    while maps and len(pages) < limit:
        m = maps.pop(0)
        if m in seen:
            continue
        seen.add(m)
        r = fetch(m, max_bytes=30_000_000)
        if r.status != 200:
            if bad is not None:
                bad[m] = r.status or r.error
            continue
        body = r.body
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        xml = body.decode("utf-8", "replace")
        locs = [html.unescape(x.strip()) for x in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", xml, re.S)]
        if "<sitemapindex" in xml:
            maps.extend(locs)
        else:
            pages.extend(locs)
        if pages and not maps:
            break
    return list(dict.fromkeys(pages))[:limit]


def cert_days_left(url):
    host = urllib.parse.urlsplit(url).hostname
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
            exp = ssl.cert_time_to_seconds(ss.getpeercert()["notAfter"])
        return int((exp - time.time()) // 86400)
    except Exception:  # noqa: BLE001
        return None


def esc(s):
    return html.escape(str(s), quote=False)


def _tg(method, data, headers=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        # Telegram's own reason ("Unauthorized", "chat not found") names no secret, so it may be logged.
        try:
            reason = json.loads(e.read()).get("description", "")
        except Exception:  # noqa: BLE001
            reason = ""
        raise TelegramError(f"Telegram {method} HTTP {e.code}: {reason}") from None


class TelegramError(Exception):
    pass


def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(msg):
    """Send an HTML-formatted message, split under Telegram's 4096-char limit.

    Outside CI with no Telegram secrets, prints instead (for local testing).
    In CI the text is never printed: the repo is public, so are its logs.
    """
    if not telegram_configured():
        print(msg if not IN_CI else "!! Telegram secrets missing — message not sent")
        return
    chunk, chunks = "", []
    lines = [part for line in msg.split("\n") for part in (line[i:i + 3800] for i in range(0, max(len(line), 1), 3800))]
    for line in lines:
        if len(chunk) + len(line) > 3800:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    chunks.append(chunk)
    for c in chunks:
        _tg("sendMessage", urllib.parse.urlencode({
            "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": c, "parse_mode": "HTML",
            "disable_web_page_preview": "true"}).encode())


def send_file(name, content, caption=""):
    """Send a text report as a Telegram document."""
    if not telegram_configured():
        if not IN_CI:
            print(f"--- {name} ---\n{content}")
        return
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", os.environ["TELEGRAM_CHAT_ID"]), ("caption", caption[:1000]), ("parse_mode", "HTML")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                 f"Content-Type: text/markdown\r\n\r\n".encode() + content.encode() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    _tg("sendDocument", b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def now_myt():
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600)) + " MYT"
