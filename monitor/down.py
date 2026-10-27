"""Server-down check (every 10 min). Alerts on down, reminds hourly, and says when it's back.

Keeps state + 8 days of history in state/state.json (carried between runs by actions/cache);
uptime_report.py reads the same file.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from common import esc, fetch, now_myt, safe_main, send, sites, text

STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "state.json")
KEEP_SECS = 8 * 86400
REMIND_SECS = 3600
SLOW_SECS = 10
# Text WordPress / PHP / Cloudflare show when the page "loads" but the shop is broken.
BROKEN_MARKERS = ("There has been a critical error", "Error establishing a database connection",
                  "Fatal error</b>", "Briefly unavailable for scheduled maintenance")


def check(site):
    r = fetch(site["url"] + "/", timeout=30)
    body = text(r)
    if r.status != 200:
        why = f"HTTP {r.status}" if r.status else (r.error or "no answer")
    elif any(m in body for m in BROKEN_MARKERS):
        why = "page shows a WordPress/PHP error"
    elif "</html>" not in body.lower():
        why = "page cut off (no </html>)"
    else:
        return True, r.secs, "slow" if r.secs > SLOW_SECS else ""
    return False, r.secs, why


def check_twice(site):
    ok, secs, why = check(site)
    if not ok:  # one retry, so a single blip doesn't wake anyone
        time.sleep(45)
        ok, secs, why = check(site)
    return ok, secs, why


def main():
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"sites": {}, "history": []}
    now = int(time.time())
    msgs = []
    picked = sites(sys.argv[1:])
    with ThreadPoolExecutor(max(len(picked), 1)) as pool:  # sites are on different servers: check them together
        results = list(pool.map(check_twice, picked))
    for site, (ok, secs, why) in zip(picked, results):
        name = site["name"]
        st = state["sites"].setdefault(site["id"], {"up": True, "since": now, "alerted": 0})
        state["history"].append([now, site["id"], int(ok), int(secs * 1000)])
        print(f"{site['label']}: {'up' if ok else 'DOWN'}")
        link = f'<a href="{site["url"]}/">{esc(site["url"])}</a>'
        if ok and not st["up"]:
            mins = (now - st["since"]) // 60
            msgs.append(f"🟢 <b>{name} is back up</b> after ~{mins} min\n{link} · {secs:.1f}s")
            st.update(up=True, since=now, alerted=0)
        elif not ok and st["up"]:
            msgs.append(f"🔴 <b>{name} is DOWN</b>\n{link}\nReason: {esc(why)}")
            st.update(up=False, since=now, alerted=now)
        elif not ok and now - st.get("alerted", 0) >= REMIND_SECS:
            mins = (now - st["since"]) // 60
            msgs.append(f"🔴 <b>{name} still DOWN</b> ({mins} min)\n{link}\nReason: {esc(why)}")
            st.update(alerted=now)
    state["history"] = [h for h in state["history"] if h[0] > now - KEEP_SECS]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)
    if msgs:
        send("\n\n".join(msgs) + f"\n\n<i>{now_myt()}</i>")


if __name__ == "__main__":
    safe_main(main)
