#!/usr/bin/env python3
"""Archive GitHub repo traffic (views) beyond GitHub's 14-day window.

GitHub's Insights > Traffic data only covers the last 14 days. This script
pulls the daily view counts from the traffic API, merges them into a
permanent CSV history, and writes a shields.io endpoint JSON for a README badge.

Env:
  TRAFFIC_TOKEN  fine-grained PAT with "Administration: Read-only" on this repo
  REPO           owner/name (set automatically by the workflow)
"""
import csv
import json
import os
import sys
import urllib.request
from pathlib import Path

OUT_DIR = Path("traffic")
CSV_PATH = OUT_DIR / "views.csv"
BADGE_PATH = OUT_DIR / "views-badge.json"
FIELDS = ["date", "views", "uniques"]


def fetch_views(repo: str, token: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/traffic/views?per=day",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return data.get("views", [])


def load_history() -> dict[str, dict]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="") as f:
        return {row["date"]: row for row in csv.DictReader(f)}


def main() -> int:
    token = os.environ.get("TRAFFIC_TOKEN")
    repo = os.environ.get("REPO")
    if not token or not repo:
        print("TRAFFIC_TOKEN and REPO must be set", file=sys.stderr)
        return 1

    history = load_history()
    for day in fetch_views(repo, token):
        date = day["timestamp"][:10]
        # GitHub keeps revising the most recent days, so the API value wins.
        history[date] = {"date": date, "views": day["count"], "uniques": day["uniques"]}

    OUT_DIR.mkdir(exist_ok=True)
    rows = [history[d] for d in sorted(history)]
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    total = sum(int(r["views"]) for r in rows)
    BADGE_PATH.write_text(json.dumps({
        "schemaVersion": 1,
        "label": "views",
        "message": f"{total:,}",
        "color": "blue",
    }) + "\n")

    since = rows[0]["date"] if rows else "n/a"
    print(f"{len(rows)} days on record since {since}; {total} total views")
    return 0


if __name__ == "__main__":
    sys.exit(main())
