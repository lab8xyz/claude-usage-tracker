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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CACHE_PATH = Path("/tmp/claude-statusline-cache.json")

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
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


def get_git_branch():
    """Get the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def format_reset_time(iso_timestamp):
    """Format reset time as local HH:MM (24h)."""
    if not iso_timestamp:
        return ""
    try:
        reset_time = datetime.fromisoformat(iso_timestamp)
        local_time = reset_time.astimezone()
        return local_time.strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def fetch_usage_cached():
    """Read usage data from shared cache written by the tray app.

    Only the tray app calls the API — the statusline just reads from cache
    to avoid burning through the very low per-token rate limit (~5 requests).
    """
    try:
        if CACHE_PATH.exists():
            cache = json.loads(CACHE_PATH.read_text())
            age = time.time() - cache.get("_ts", 0)
            if age < 600:  # Accept cache up to 10 min old
                return cache
            return {"_error": f"cache stale ({int(age)}s)"}
    except (json.JSONDecodeError, OSError):
        pass
    return {"_error": "no data (is tray app running?)"}


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

    # Git branch
    cwd = stdin_data.get("cwd") or stdin_data.get("workspace", {}).get("current_dir")
    branch = None
    if cwd:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=2, cwd=cwd
            )
            if result.returncode == 0:
                branch = result.stdout.strip()
        except Exception:
            pass
    if not branch:
        branch = get_git_branch()

    if "_error" in usage:
        # Fallback: just show model + context + branch
        branch_part = f" | \u2387  {CYAN}{branch}{RESET}" if branch else ""
        print(f" {BOLD}Claude Usage{RESET}{branch_part} | £{total_cost:.2f} | ctx {ctx_pct}% | {DIM}{usage['_error']}{RESET}")
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

    # Branch with git icon (U+2387 Alternative Key Symbol)
    cwd_name = os.path.basename(cwd) if cwd else ""
    branch_part = f" | \u2387  {CYAN}{branch}{RESET}" if branch else ""
    cwd_part = f" | {MAGENTA}{cwd_name}{RESET}" if cwd_name else ""

    # Line 1: Progress bars
    bar_parts = [
        f" 5h: {session_pct:.0f}% {make_bar(session_pct, 10)}",
        f"7d: {weekly_pct:.0f}% {make_bar(weekly_pct, 10)}",
        f"ctx: {ctx_pct}% {make_bar(ctx_pct, 8)}",
    ]
    print(" | ".join(bar_parts))

    # Line 2: Info
    info_parts = [
        f" {status_dot} {BOLD}Claude Usage{RESET}{branch_part}{cwd_part}",
        f"{model_name} £{total_cost:.2f}",
        f"→ Reset: {format_reset_time(session_reset)}",
    ]
    print(" | ".join(info_parts))


if __name__ == "__main__":
    main()
