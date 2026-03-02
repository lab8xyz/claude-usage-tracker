#!/usr/bin/env python3
"""Claude Code statusline script.

Shows usage limits, model, cost, and context info at the bottom
of the Claude Code terminal. Reads JSON from stdin (piped by Claude Code)
and fetches usage data from the API with caching.

Install: Add to ~/.claude/settings.json:
  "statusLine": {
    "type": "command",
    "command": "python3 ~/Programming/claudeuseage/claude-statusline.py"
  }
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
STATUS_API_URL = "https://status.claude.com/api/v2/status.json"
CACHE_PATH = Path("/tmp/claude-statusline-cache.json")
CACHE_TTL_SECONDS = 60

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BG_RED = "\033[41m"


def color_for_pct(pct):
    if pct >= 90:
        return RED
    if pct >= 75:
        return YELLOW
    return GREEN


def format_countdown(iso_timestamp):
    if not iso_timestamp:
        return "?"
    try:
        reset_time = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(timezone.utc)
        delta = reset_time - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return "now"
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "?"


def calc_pacing(actual_pct, reset_iso, window_hours):
    """Calculate pacing. Returns (elapsed, total, unit, expected_pct, pace_diff) or None."""
    if not reset_iso:
        return None
    try:
        reset_time = datetime.fromisoformat(reset_iso)
        now = datetime.now(timezone.utc)
        remaining = reset_time - now
        hours_remaining = max(remaining.total_seconds() / 3600, 0)
        hours_elapsed = window_hours - hours_remaining
        if hours_elapsed < 0:
            hours_elapsed = 0
        expected_pct = (hours_elapsed / window_hours) * 100
        pace_diff = actual_pct - expected_pct
        if window_hours <= 24:
            return (hours_elapsed, window_hours, "h", expected_pct, pace_diff)
        else:
            return (hours_elapsed / 24, window_hours / 24, "d", expected_pct, pace_diff)
    except (ValueError, TypeError):
        return None


def pace_indicator(pace_diff):
    """Return a colored pace arrow/indicator."""
    if pace_diff > 15:
        return f"{RED}{BOLD}++{RESET}"
    if pace_diff > 10:
        return f"{RED}+{RESET}"
    if pace_diff > 5:
        return f"{YELLOW}+{RESET}"
    if pace_diff < -5:
        return f"{GREEN}-{RESET}"
    return f"{GREEN}={RESET}"


def load_credentials():
    try:
        with open(CREDENTIALS_PATH) as f:
            creds = json.load(f)
        oauth = creds.get("claudeAiOauth", {})
        return oauth.get("accessToken")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def fetch_usage_cached():
    """Fetch usage data with file-based caching."""
    # Check cache
    try:
        if CACHE_PATH.exists():
            cache = json.loads(CACHE_PATH.read_text())
            if time.time() - cache.get("_ts", 0) < CACHE_TTL_SECONDS:
                return cache
    except (json.JSONDecodeError, OSError):
        pass

    # Fetch fresh
    token = load_credentials()
    if not token:
        return {"_error": "no token"}

    try:
        import requests
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "claude-code/2.1.5",
            "anthropic-beta": "oauth-2025-04-20",
        }
        resp = requests.get(USAGE_API_URL, headers=headers, timeout=10)
        if resp.status_code != 200:
            return {"_error": f"HTTP {resp.status_code}"}
        data = resp.json()
        data["_ts"] = time.time()

        # Also fetch status
        try:
            sr = requests.get(STATUS_API_URL, timeout=5)
            if sr.status_code == 200:
                sd = sr.json()
                data["_status"] = sd.get("status", {}).get("indicator", "unknown")
        except Exception:
            data["_status"] = "unknown"

        # Write cache
        try:
            CACHE_PATH.write_text(json.dumps(data))
        except OSError:
            pass

        return data
    except Exception as e:
        return {"_error": str(e)}


def get_utilization(data, key):
    entry = data.get(key)
    if not entry or not isinstance(entry, dict):
        return 0.0
    val = entry.get("utilization", 0)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def get_reset(data, key):
    entry = data.get(key)
    if not entry or not isinstance(entry, dict):
        return None
    return entry.get("resets_at")


def make_bar(pct, width=10):
    """Create a text-based progress bar."""
    filled = int(pct / 100 * width)
    filled = max(0, min(filled, width))
    color = color_for_pct(pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"{color}{bar}{RESET}"


def main():
    # Read Claude Code's JSON from stdin
    try:
        stdin_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        stdin_data = {}

    # Extract info from Claude Code
    model = stdin_data.get("model", {})
    model_name = model.get("display_name", "?")

    cost = stdin_data.get("cost", {})
    total_cost = cost.get("total_cost_usd", 0)

    ctx = stdin_data.get("context_window", {})
    ctx_pct = ctx.get("used_percentage", 0)

    # Fetch usage data (cached)
    usage = fetch_usage_cached()

    if "_error" in usage:
        # Fallback: just show model + context
        print(f" {BOLD}{model_name}{RESET} | ${total_cost:.2f} | ctx {ctx_pct}% | {DIM}usage: {usage['_error']}{RESET}")
        return

    # Parse usage
    session_pct = get_utilization(usage, "five_hour")
    session_reset = get_reset(usage, "five_hour")
    weekly_pct = get_utilization(usage, "seven_day")
    weekly_reset = get_reset(usage, "seven_day")

    # Pacing
    session_pace = calc_pacing(session_pct, session_reset, 5)
    weekly_pace = calc_pacing(weekly_pct, weekly_reset, 168)

    session_pace_ind = pace_indicator(session_pace[4]) if session_pace else ""
    weekly_pace_ind = pace_indicator(weekly_pace[4]) if weekly_pace else ""

    # Context bar
    ctx_color = color_for_pct(ctx_pct)

    # Status indicator
    status = usage.get("_status", "unknown")
    if status == "none":
        status_dot = f"{GREEN}●{RESET}"
    elif status == "minor":
        status_dot = f"{YELLOW}●{RESET}"
    elif status in ("major", "critical"):
        status_dot = f"{RED}●{RESET}"
    else:
        status_dot = f"{DIM}●{RESET}"

    # Build output line
    parts = [
        f" {status_dot} {BOLD}{model_name}{RESET}",
        f"${total_cost:.2f}",
        f"ctx {ctx_color}{ctx_pct}%{RESET}",
        f"5h: {color_for_pct(session_pct)}{session_pct:.0f}%{RESET}{session_pace_ind} ({format_countdown(session_reset)})",
        f"7d: {color_for_pct(weekly_pct)}{weekly_pct:.0f}%{RESET}{weekly_pace_ind} ({format_countdown(weekly_reset)})",
    ]

    print(" | ".join(parts))


if __name__ == "__main__":
    main()
