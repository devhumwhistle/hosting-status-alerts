#!/usr/bin/env python3
"""Watch a Sorry status page and push what changes to a phone, through ntfy.

Each run reads the public status API, compares it with state.json from the
run before, and sends one notification for every notice that is new or has a
new update. It can also check that a website answers.

Settings come from the environment (repository secrets, in the workflow):

  NTFY_TOPIC    Required. The ntfy topic to publish to. Anyone who knows it
                can read and send, so it is a secret.
  WATCH_SERVER  Optional. A server name such as abc12. Notices naming other
                servers and not this one are skipped; notices naming it are
                marked urgent.
  WATCH_SITE    Optional. A URL that should answer. Two failed checks in a
                row send an alert, and the first success after that another.
  STATUS_API    Optional. The status API, https://status.guru.co.uk/api/v1
                unless set.
  NTFY_SERVER   Optional. https://ntfy.sh unless set.

  python3 check.py             check once
  python3 check.py --test      also send a test notification
  python3 check.py --dry-run   print notifications instead of sending them

The first run only records what is already there, so it sends nothing. The
log says how many notifications went out and nothing more: a public
repository's run logs are public.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API = os.environ.get("STATUS_API", "https://status.guru.co.uk/api/v1").rstrip("/")
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
SERVER = os.environ.get("WATCH_SERVER", "").strip().lower()
SITE = os.environ.get("WATCH_SITE", "").strip()
STATE = Path(__file__).with_name("state.json")
DRY_RUN = "--dry-run" in sys.argv
UK = ZoneInfo("Europe/London")

# Server names on this status page look like grh67 or gsp7.
SERVER_CODE = re.compile(r"\bg[rs][hp]\d+\b", re.I)

ACTIVE = {"investigating", "identified"}
DONE = {"resolved", "false_alarm", "complete"}
LABEL = {
    "investigating": "Investigating",
    "identified": "Cause found",
    "recovering": "Recovering",
    "resolved": "Resolved",
    "false_alarm": "False alarm",
    "scheduled": "Scheduled",
    "underway": "Under way",
    "complete": "Complete",
    "cancelled": "Cancelled",
}


def get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "status-alerts"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def send(title, message, priority=3, tags=(), click=None):
    body = {"topic": TOPIC, "title": title, "message": message, "priority": priority, "tags": list(tags)}
    if click:
        body["click"] = click
    if DRY_RUN:
        print(json.dumps({k: v for k, v in body.items() if k != "topic"}, indent=2, ensure_ascii=False))
        return True
    req = urllib.request.Request(
        NTFY + "/", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "status-alerts"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        print("a notification could not be sent; it will be tried again next run")
        return False


def when(iso):
    if not iso:
        return ""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UK)
    return d.strftime("%a %-d %b, %H:%M")


def span(start, end):
    if not start:
        return ""
    if not end:
        return "From " + when(start)
    a, b = when(start), when(end)
    return a + " to " + (b.split(", ")[1] if a.split(", ")[0] == b.split(", ")[0] else b)


def servers_named(notice):
    words = " ".join([notice.get("subject") or ""] + [u.get("content") or "" for u in notice.get("updates") or []])
    return {s.lower() for s in SERVER_CODE.findall(words)}


def alert_for(n):
    """The notification for a notice, from its full record."""
    state = n.get("state")
    ours = bool(SERVER) and SERVER in servers_named(n)
    updates = sorted(n.get("updates") or [], key=lambda u: u.get("created_at") or "")
    latest = updates[-1]["content"] if updates else ""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", latest.replace("\r\n", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > 600:
        text = text[:600].rsplit(" ", 1)[0] + "…"

    if n.get("type") == "planned":
        window = span(n.get("began_at") or n.get("begins_at"), n.get("ended_at") or n.get("ends_at"))
    else:
        window = span(n.get("began_at"), n.get("ended_at"))
    services = ", ".join(c["name"] for c in n.get("components") or [])
    head = " · ".join(x for x in [window, services] if x)

    if n.get("type") == "unplanned" and state in ACTIVE:
        priority, tags = (5 if ours else 4), ["warning"]
    elif state in DONE:
        priority, tags = 2, ["white_check_mark"]
    elif state == "cancelled":
        priority, tags = 2, ["x"]
    elif n.get("type") == "planned":
        priority, tags = (4 if ours else 3), ["wrench"]
    else:
        priority, tags = 3, ["information_source"]

    label = LABEL.get(state, "Notice")
    title = ("Your server · " if ours else "") + label + ": " + n.get("subject", "Status notice")
    message = "\n\n".join(x for x in [head, text] if x) or label
    return dict(title=title, message=message, priority=priority, tags=tags, click=n.get("url"))


def fingerprint(n):
    latest = n.get("latest_update") or {}
    return f'{n.get("state")}|{latest.get("created_at") or n.get("updated_at")}'


def site_answers(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (status-alerts)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def main():
    if not TOPIC and not DRY_RUN:
        sys.exit("NTFY_TOPIC is not set")
    first_run = not STATE.exists()
    state = json.loads(STATE.read_text()) if not first_run else {}
    seen = state.get("notices", {})
    sent = 0

    if "--test" in sys.argv:
        sent += send("Status alerts are on",
                     "This is a test. You'll be told here when an incident or maintenance starts, changes or ends.",
                     priority=3, tags=["white_check_mark"])

    try:
        notices = get_json(API + "/notices")["notices"]
        feed_ok = True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        print("the status feed could not be read this run")
        notices, feed_ok = [], False

    if feed_ok:
        kept = {}
        for n in notices:
            key, fp = str(n["id"]), fingerprint(n)
            if first_run or seen.get(key) == fp or n.get("timeline_state") == "past_distant":
                kept[key] = fp
                continue
            try:
                full = get_json(f'{API}/notices/{n["id"]}')["notice"]
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
                if key in seen:
                    kept[key] = seen[key]
                continue
            named = servers_named(full)
            if SERVER and named and SERVER not in named:
                kept[key] = fp  # somebody else's server
                continue
            if send(**alert_for(full)):
                kept[key] = fp
                sent += 1
            elif key in seen:
                kept[key] = seen[key]
        state["notices"] = kept

    # The site counts as down only when the feed, on another host, answered
    # the same run: if both fail, it is this end's connection.
    if SITE:
        up = site_answers(SITE)
        failures = 0 if up else state.get("site_failures", 0) + (1 if feed_ok else 0)
        alerted = state.get("site_alerted", False)
        if not up and failures >= 2 and not alerted:
            if send("Website isn't answering", "Two checks in a row have failed. Tap to try it yourself.",
                    priority=5, tags=["rotating_light"], click=SITE):
                alerted, sent = True, sent + 1
        elif up and alerted:
            if send("Website is answering again", "It's back up.", priority=3, tags=["white_check_mark"], click=SITE):
                alerted, sent = False, sent + 1
        state["site_failures"], state["site_alerted"] = failures, alerted

    if not DRY_RUN:
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(("first run: recorded what is there" if first_run else "checked") + f", {sent} sent")


if __name__ == "__main__":
    main()
