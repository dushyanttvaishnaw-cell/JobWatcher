#!/usr/bin/env python3
"""Daily summary + watchdog.

    python summary.py            # send the JOB SEARCH SUMMARY for the last 24h, write reports/YYYY-MM-DD.md
    python summary.py --watchdog # alert if the 5-minute watcher hasn't completed a cycle recently
"""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path

from watcher import ALERTS_PATH, HEALTH_PATH, ROOT, UTC, load_json, parse_ts, send_plain

STALE_MINUTES = 40  # GitHub's 5-minute schedule is often delayed; 40 min means something is wrong


def load_alerts():
    if not ALERTS_PATH.exists():
        return []
    out = []
    for line in ALERTS_PATH.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def summary(now=None, send=True) -> str:
    now = now or dt.datetime.now(UTC)
    allrecs = load_alerts()
    day = [r for r in allrecs if (parse_ts(r["alerted_at"]) or now) >= now - dt.timedelta(hours=24)]
    strong = [r for r in day if r.get("strong")]
    high = [r for r in day if r.get("match") == "High"]
    moderate = [r for r in day if r.get("match") == "Moderate"]
    companies = collections.Counter(r["company"] for r in day).most_common(8)
    roles = collections.Counter(_role(r["title"]) for r in day).most_common(6)
    skills = collections.Counter(s for r in day for s in r.get("skills", [])).most_common(10)
    health = load_json(HEALTH_PATH, {})
    boards = health.get("boards", {})
    broken = sorted(k for k, v in boards.items() if v.get("fail_streak", 0) >= 12)
    ages = [r["age_min"] for r in day if r.get("age_min") is not None]
    med_age = sorted(ages)[len(ages) // 2] if ages else None

    attention = sorted(strong or high, key=lambda r: -r.get("score", 0))[:10]
    lines = ["JOB SEARCH SUMMARY", f"{now:%A, %B %d, %Y}", "",
             f"New jobs found: {len(day)}",
             f"Strong matches: {len(strong)}",
             f"High matches: {len(high)}",
             f"Moderate matches: {len(moderate)}",
             f"Jobs already sent (all time): {len(allrecs)}", "",
             "Top companies hiring:"] + [f"• {c} ({n})" for c, n in companies] + ["",
             "Most common roles:"] + [f"• {c} ({n})" for c, n in roles] + ["",
             "Most common skills:"] + [f"• {c} ({n})" for c, n in skills] + ["",
             "Jobs requiring my attention:"] + \
            [f"• {'🔥 ' if r.get('strong') else ''}{r['title']} — {r['company']} (score {r['score']})\n  {r['url']}"
             for r in attention] + ["",
             "Monitoring health:",
             f"• Last cycle: {health.get('last_run', 'never')}",
             f"• Sources OK last cycle: {health.get('boards_ok', 0)}; failing: {health.get('boards_failed', 0)}",
             f"• Median alert age (posted → alerted): {med_age:.0f} min" if med_age is not None
             else "• Median alert age: n/a (no timestamped alerts today)"]
    if broken:
        lines.append("• Sources failing for 1h+ (fix or remove in companies.txt): " + ", ".join(broken[:15]))
    text = "\n".join(lines)

    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"{now:%Y-%m-%d}.md").write_text("```\n" + text + "\n```\n")
    print(text)
    if send:
        send_plain(f"📊 Job search summary — {len(day)} new, {len(strong)} strong", text, 3, ["bar_chart"])
    return text


def _role(title: str) -> str:
    t = title.lower()
    for key in ("supply chain", "procurement", "inventory", "demand plan", "logistics", "business intelligence",
                "bi analyst", "business systems", "business analyst", "data analyst", "operations analyst",
                "product", "project", "program", "reporting", "process"):
        if key in t:
            return key.title()
    return "Other analyst"


def watchdog(now=None) -> bool:
    now = now or dt.datetime.now(UTC)
    health = load_json(HEALTH_PATH, {})
    last = parse_ts(health.get("last_run"))
    if last is None or (now - last).total_seconds() > STALE_MINUTES * 60:
        mins = "never" if last is None else f"{(now - last).total_seconds() / 60:.0f} min ago"
        send_plain("🛑 Job monitoring has STOPPED",
                   f"The watcher's last successful cycle was {mins}. "
                   "Check the repo's Actions tab (a workflow may be disabled or failing).", 5, ["warning"])
        print("STALE:", mins)
        return False
    print("Watcher healthy; last cycle", health.get("last_run"))
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchdog", action="store_true")
    a = ap.parse_args()
    watchdog() if a.watchdog else summary()
