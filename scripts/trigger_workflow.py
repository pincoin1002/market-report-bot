#!/usr/bin/env python3
"""Helper script to trigger GitHub Action workflows instantly via repository_dispatch.

Usage:
    python scripts/trigger_workflow.py <report_type>

Requires environment variable:
    GITHUB_TOKEN (Personal Access Token with 'repo' scope)
"""

import os
import sys
import json
import urllib.request

EVENT_TYPES = {
    "tw_open": "trigger-tw-open",
    "tw_close": "trigger-tw-close",
    "us_open": "trigger-us-open",
    "us_close": "trigger-us-close",
}

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/trigger_workflow.py <report_type>")
        print("Available report types: tw_open, tw_close, us_open, us_close")
        sys.exit(1)

    report_type = sys.argv[1]
    event_type = EVENT_TYPES.get(report_type)
    if not event_type:
        print(f"Error: Invalid report type '{report_type}'")
        print("Available report types: tw_open, tw_close, us_open, us_close")
        sys.exit(1)

    # Try loading from .env file if GITHUB_TOKEN is not in env
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        # Try reading .env file manually
        from pathlib import Path
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip("\"").strip("\'")
                    break

    if not token:
        print("Error: GITHUB_TOKEN environment variable is not set and not found in .env", file=sys.stderr)
        print("Please generate a Personal Access Token (classic) on GitHub with 'repo' scope,", file=sys.stderr)
        print("and set it as: export GITHUB_TOKEN='your_token'", file=sys.stderr)
        sys.exit(1)

    # Repository details
    repo = "pincoin1002/market-report-bot"
    url = f"https://api.github.com/repos/{repo}/dispatches"

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "MarketReportBot-Trigger",
        "Content-Type": "application/json",
    }

    body = json.dumps({
        "event_type": event_type
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.getcode()
            if status in (204, 200, 201):
                print(f"✓ Successfully dispatched event '{event_type}' to {repo} (Status: {status})")
                print("GitHub Actions runner will start the workflow in a few seconds.")
            else:
                print(f"⚠️ Dispatch returned status {status}")
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="ignore")
        print(f"❌ Failed to dispatch workflow: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(f"Details: {body_err}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
