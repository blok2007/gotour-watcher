#!/usr/bin/env python3
"""
GO Tour: Alola - Los Angeles ticket drop watcher.

Polls the official Pokemon GO pages (and Leek Duck as a backup) and pushes an
alert to your phone the moment the 2027 LA ticket page / announcement appears.

Zero dependencies: Python 3.8+ standard library only.

Usage:
    python3 gotour_watch.py --ntfy-topic YOUR_SECRET_TOPIC          # run forever
    python3 gotour_watch.py --ntfy-topic YOUR_SECRET_TOPIC --test   # send a test push and exit
    python3 gotour_watch.py --once                                   # one check, print results

Notifications (use any combination):
  * ntfy push to your phone  (--ntfy-topic, install the free "ntfy" app and subscribe to the same topic)
  * email via SMTP           (env vars: SMTP_USER, SMTP_PASS [Gmail app password], ALERT_EMAIL_TO)
  * desktop popup + sound    (automatic on macOS)
"""

import argparse
import hashlib
import html
import json
import os
import random
import re
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

BUY_URL = "https://pokemongo.com/gotour/losangeles/tickets"

# ---------------------------------------------------------------------------
# Sources. Each one has a check() that takes the page text and returns
# (fired: bool, detail: str). Keep the logic dumb and robust: look for the
# word "Alola" showing up on pages that currently still describe Kalos (2026),
# or for "Tickets" language appearing where it currently says "next Season".
# ---------------------------------------------------------------------------


def _has(text, *words):
    t = text.lower()
    return all(w.lower() in t for w in words)


def check_official_la(text):
    # Today this page still describes "Kalos - Los Angeles" (Feb 2026).
    # When Scopely flips it to the 2027 event, "Alola" will appear.
    if _has(text, "alola"):
        on_sale = _has(text, "tickets available") or _has(text, "buy tickets")
        return True, "Official LA GO Tour page now mentions Alola" + (
            " and shows ticket purchase language" if on_sale else ""
        )
    return False, "still the 2026 Kalos page"


def check_official_tickets(text):
    if _has(text, "alola"):
        return True, "Official LA *tickets* page switched to Alola"
    return False, "tickets page still 2026"


def check_official_gotour_hub(text):
    if _has(text, "alola", "los angeles") and (
        _has(text, "ticket") or _has(text, "buy")
    ):
        return True, "GO Tour hub page mentions Alola + Los Angeles + tickets"
    return False, "no Alola LA ticket info on hub"


def check_leekduck(text):
    # Leek Duck currently says tickets "will be available next Season".
    still_waiting = _has(text, "available next season")
    has_price = bool(re.search(r"ticket price[^$]{0,40}\$\s?\d", text, re.I))
    if has_price or (not still_waiting and _has(text, "ticket")):
        return True, "Leek Duck event page updated with ticket info"
    return False, "Leek Duck still says 'next Season'"


SOURCES = [
    {
        "name": "official_la",
        "url": "https://pokemongo.com/gotour/losangeles",
        "check": check_official_la,
    },
    {
        "name": "official_tickets",
        "url": "https://pokemongo.com/gotour/losangeles/tickets",
        "check": check_official_tickets,
    },
    {
        "name": "official_gotour_hub",
        "url": "https://pokemongo.com/gotour",
        "check": check_official_gotour_hub,
    },
    {
        "name": "leekduck",
        "url": "https://leekduck.com/events/pokemon-go-tour-alola-los-angeles-2027/",
        "check": check_leekduck,
    },
]

# News index: alert on any NEW headline mentioning Alola / GO Tour.
NEWS_URL = "https://pokemongo.com/en/news"
NEWS_KEYWORDS = re.compile(r"alola|go tour|gotour", re.I)


# ---------------------------------------------------------------------------
# Fetching / parsing
# ---------------------------------------------------------------------------


def fetch(url, timeout=20):
    sep = "&" if "?" in url else "?"
    bust = f"{url}{sep}_={int(time.time())}"  # dodge stale CDN caches
    req = urllib.request.Request(
        bust,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode("utf-8", errors="replace")


def html_to_text(page):
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    # Keep JSON blobs inside __NEXT_DATA__ etc. since sites often render from them
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html.unescape(page)
    return re.sub(r"\s+", " ", page)


def extract_news_headlines(page):
    by_url = {}
    for m in re.finditer(r'href="(/[a-z\-]*/?news/[^"#?]+)"[^>]*>(.*?)</a>', page, re.I | re.S):
        title = html_to_text(m.group(2)).strip()
        slug = m.group(1)
        if NEWS_KEYWORDS.search(title) or NEWS_KEYWORDS.search(slug):
            by_url[slug.split("/news/")[-1]] = f"{title or slug} | https://pokemongo.com{slug}"
    # Fallback: slugs anywhere in the page (JS-rendered lists)
    for slug in re.findall(r'(/(?:[a-z]{2}(?:-[A-Z]{2})?/)?news/[a-z0-9\-]+)', page):
        if NEWS_KEYWORDS.search(slug):
            by_url.setdefault(slug.split("/news/")[-1], f"{slug} | https://pokemongo.com{slug}")
    return set(by_url.values())


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def notify(title, body, cfg, urgent=True):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{stamp}] *** ALERT *** {title}\n{body}\n", flush=True)

    if cfg.ntfy_topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{cfg.ntfy_topic}",
                data=body.encode(),
                headers={
                    "Title": title.encode("ascii", "ignore").decode(),
                    "Priority": "5" if urgent else "3",
                    "Tags": "rotating_light,ticket" if urgent else "information_source",
                    "Click": BUY_URL,
                    "Actions": f"view, Open ticket page, {BUY_URL}",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:  # noqa: BLE001
            print(f"  ntfy failed: {e}", flush=True)

    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("ALERT_EMAIL_TO")
    if user and pw and to:
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"], msg["To"] = user, to
            msg.set_content(f"{body}\n\nBuy: {BUY_URL}")
            host = os.getenv("SMTP_HOST", "smtp.gmail.com")
            with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context()) as s:
                s.login(user, pw)
                s.send_message(msg)
        except Exception as e:  # noqa: BLE001
            print(f"  email failed: {e}", flush=True)

    if sys.platform == "darwin":
        safe = lambda s: s.replace('"', "'")  # noqa: E731
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe(body[:200])}" with title "{safe(title)}" sound name "Glass"'],
            check=False,
        )
        if urgent:
            for _ in range(3):
                subprocess.run(["afplay", "/System/Library/Sounds/Sosumi.aiff"], check=False)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {"fired": {}, "news_seen": [], "fail_counts": {}, "news_baselined": False}


def save_state(path, state):
    Path(path).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_once(cfg, state, verbose=True):
    alerts = []

    for src in SOURCES:
        name = src["name"]
        try:
            text = html_to_text(fetch(src["url"]))
            state["fail_counts"][name] = 0
        except Exception as e:  # noqa: BLE001
            n = state["fail_counts"].get(name, 0) + 1
            state["fail_counts"][name] = n
            if verbose:
                print(f"  [{name}] fetch error #{n}: {e}", flush=True)
            if n == 10:  # tell the user once if a source goes blind
                notify("GO Tour watcher: source failing",
                       f"{name} has failed 10 checks in a row ({e}). Other sources still running.",
                       cfg, urgent=False)
            continue

        fired, detail = src["check"](text)
        if verbose:
            print(f"  [{name}] {'FIRED' if fired else 'ok'} - {detail}", flush=True)
        if fired and not state["fired"].get(name):
            state["fired"][name] = datetime.now().isoformat()
            alerts.append((detail, src["url"]))

    try:
        heads = extract_news_headlines(fetch(NEWS_URL))
        seen = set(state["news_seen"])
        new = heads - seen
        if not state["news_baselined"]:
            state["news_baselined"] = True  # first run: don't alert on old posts
            new = set()
        state["news_seen"] = sorted(seen | heads)
        if verbose:
            print(f"  [news] {len(heads)} GO Tour headlines, {len(new)} new", flush=True)
        for h in sorted(new):
            title, url = h.split(" | ", 1)
            alerts.append((f"New Pokemon GO news post: {title}", url))
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"  [news] fetch error: {e}", flush=True)

    for detail, url in alerts:
        notify("GO TOUR LA: TICKETS MAY BE LIVE", f"{detail}\n{url}\nBuy in the app: Main Menu > Events", cfg)

    return alerts


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ntfy-topic", default=os.getenv("NTFY_TOPIC"),
                   help="secret ntfy.sh topic name (make it long and random)")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks (default 60)")
    p.add_argument("--state", default=str(Path(__file__).with_name("gotour_state.json")))
    p.add_argument("--once", action="store_true", help="run one check and exit")
    p.add_argument("--test", action="store_true", help="send a test notification and exit")
    p.add_argument("--reset", action="store_true", help="forget previous alerts")
    cfg = p.parse_args()

    if cfg.test:
        notify("GO Tour watcher test", "If you see this on your phone, alerts are working.", cfg)
        return

    state = load_state(cfg.state)
    if cfg.reset:
        state = load_state("/nonexistent")

    if cfg.once:
        run_once(cfg, state)
        # Daily "still alive" ping, tracked in the state file (used by the cloud runner)
        last = state.get("last_heartbeat", 0)
        if last == 0:
            notify("GO Tour watcher started", "Cloud watcher is running. You'll get a ping once a day.", cfg, urgent=False)
            state["last_heartbeat"] = time.time()
        elif time.time() - last > 24 * 3600:
            notify("GO Tour watcher alive", "Still watching. No ticket drop yet.", cfg, urgent=False)
            state["last_heartbeat"] = time.time()
        save_state(cfg.state, state)
        return

    print(f"Watching {len(SOURCES)} pages + news every ~{cfg.interval}s. Ctrl+C to stop.", flush=True)
    notify("GO Tour watcher started", "Watching for GO Tour: Alola - LA tickets.", cfg, urgent=False)
    last_heartbeat = time.time()
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] checking...", flush=True)
        run_once(cfg, state)
        save_state(cfg.state, state)
        if time.time() - last_heartbeat > 24 * 3600:  # daily "still alive" ping
            notify("GO Tour watcher alive", "Still watching. No ticket drop yet.", cfg, urgent=False)
            last_heartbeat = time.time()
        time.sleep(cfg.interval + random.uniform(-10, 10))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
