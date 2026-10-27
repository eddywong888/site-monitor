"""Daily uptime + response-time report, built from the down check's history."""
import json
import math
import time

from common import cert_days_left, esc, now_myt, safe_main, send, sites
from down import STATE


def pct(rows):
    return f"{100 * sum(r[2] for r in rows) / len(rows):.2f}%" if rows else "no data"


def outages(rows):
    """Count runs of consecutive failed checks."""
    n, prev = 0, 1
    for r in rows:
        n += prev and not r[2]
        prev = r[2]
    return n


def main():
    try:
        with open(STATE) as f:
            history = json.load(f)["history"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        history = []
    now = time.time()
    lines = [f"📊 <b>Daily uptime report</b> — {now_myt()}"]
    for site in sites():
        rows = sorted(h for h in history if h[1] == site["id"])
        day = [r for r in rows if r[0] > now - 86400]
        week = [r for r in rows if r[0] > now - 7 * 86400]
        ms = sorted(r[3] for r in day if r[2])
        avg = f"{sum(ms) / len(ms) / 1000:.2f}s" if ms else "–"
        p95 = f"{ms[math.ceil(len(ms) * 0.95) - 1] / 1000:.2f}s" if ms else "–"
        ssl_days = cert_days_left(site["url"])
        ssl_txt = "SSL ?" if ssl_days is None else f"SSL {ssl_days}d" + (" ⚠️" if ssl_days < 14 else "")
        flag = "✅" if day and all(r[2] for r in day) else ("⚠️" if day else "❔")
        lines.append(
            f"\n{flag} <b>{site['name']}</b>\n"
            f"24h {pct(day)} ({len(day)} checks, {outages(day)} outage) · 7d {pct(week)} ({outages(week)} outage)\n"
            f"Response avg {avg} · p95 {p95} · {esc(ssl_txt)}")
        print(f"{site['label']}: 24h {pct(day)}")
    send("\n".join(lines))


if __name__ == "__main__":
    safe_main(main)
