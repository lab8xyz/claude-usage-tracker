#!/usr/bin/env python3
"""Claude Usage Tracker - Linux Mint system tray application.

Monitors Claude AI usage limits in real-time via the system tray.
Reads OAuth credentials from Claude Code CLI (~/.claude/.credentials.json).
"""

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cairo
import gi
import requests

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("XApp", "1.0")
gi.require_version("Notify", "0.7")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Notify, XApp

# --- Constants ---

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_API_URL = "https://api.anthropic.com/api/oauth/profile"
ACCOUNT_API_URL = "https://api.anthropic.com/api/oauth/account"
STATUS_API_URL = "https://status.claude.com/api/v2/status.json"
POLL_INTERVAL_SECONDS = 180
STATUSLINE_CACHE_PATH = Path("/tmp/claude-statusline-cache.json")
ICON_SIZE = 24
APP_ID = "claude-usage-tracker"
APP_NAME = "Claude Usage Tracker"

# Notification thresholds: every 5% from 75 onwards, each fires once per reset cycle
NOTIFY_THRESHOLDS = list(range(75, 101, 5))  # [75, 80, 85, 90, 95, 100]

# Pacing alerts: first at +10% ahead, then every 5% (+15, +20, +25...)
PACE_FIRST_THRESHOLD = 10
PACE_STEP = 5
# Grace period: ignore pacing alerts within this many minutes of a new window
PACE_GRACE_MINUTES = 10

# Colors
COLOR_GREEN = (0.30, 0.69, 0.31)   # #4CAF50
COLOR_YELLOW = (1.0, 0.76, 0.03)   # #FFC107
COLOR_RED = (0.96, 0.26, 0.21)     # #F44336
COLOR_BG = (0.25, 0.25, 0.25)      # dark gray track
COLOR_TEXT = (1.0, 1.0, 1.0)       # white text


def usage_color(pct):
    """Return an RGB tuple based on usage percentage."""
    if pct >= 90:
        return COLOR_RED
    if pct >= 75:
        return COLOR_YELLOW
    return COLOR_GREEN


def format_countdown(iso_timestamp):
    """Format an ISO timestamp as a human-readable countdown."""
    if not iso_timestamp:
        return "N/A"
    try:
        reset_time = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(timezone.utc)
        delta = reset_time - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return "Now"
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "N/A"


def format_time(iso_timestamp):
    """Format an ISO timestamp as a local time string."""
    if not iso_timestamp:
        return "N/A"
    try:
        reset_time = datetime.fromisoformat(iso_timestamp)
        local_time = reset_time.astimezone()
        return local_time.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return "N/A"


def normalize_reset_time(iso_timestamp):
    """Truncate reset time to the minute for stable comparison.

    The API sometimes returns slightly different fractional seconds
    between polls, which would incorrectly clear notification tracking.
    """
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.replace(second=0, microsecond=0).isoformat()
    except (ValueError, TypeError):
        return iso_timestamp


def calc_pacing(actual_pct, reset_iso, window_hours):
    """Calculate pacing for a usage window.

    Args:
        actual_pct: current utilization percentage
        reset_iso: ISO timestamp when the window resets
        window_hours: total window duration in hours (5 for session, 168 for weekly)

    Returns (elapsed, total, unit, expected_pct, pace_diff) or None.
    - elapsed: time elapsed in the natural unit (hours or days)
    - total: window size in the natural unit
    - unit: 'h' or 'd'
    - expected_pct: ideal usage % for even distribution
    - pace_diff: actual - expected (positive = ahead/burning fast)
    """
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

        # Use hours for short windows, days for weekly
        if window_hours <= 24:
            return (hours_elapsed, window_hours, "h", expected_pct, pace_diff)
        else:
            days_elapsed = hours_elapsed / 24.0
            days_total = window_hours / 24.0
            return (days_elapsed, days_total, "d", expected_pct, pace_diff)
    except (ValueError, TypeError):
        return None


# --- Icon Rendering ---

def render_icon(session_pct):
    """Render a tray icon as a GdkPixbuf with an arc showing session usage."""
    size = ICON_SIZE
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)

    center = size / 2.0
    radius = (size / 2.0) - 1.5
    line_width = 3.0

    # Background track (full circle)
    ctx.set_line_width(line_width)
    ctx.set_source_rgb(*COLOR_BG)
    ctx.arc(center, center, radius, 0, 2 * math.pi)
    ctx.stroke()

    # Usage arc (clockwise from top)
    if session_pct > 0:
        color = usage_color(session_pct)
        ctx.set_source_rgb(*color)
        ctx.set_line_width(line_width)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        start_angle = -math.pi / 2
        end_angle = start_angle + (2 * math.pi * min(session_pct, 100) / 100)
        ctx.arc(center, center, radius, start_angle, end_angle)
        ctx.stroke()

    # Center text (percentage number)
    ctx.set_source_rgb(*COLOR_TEXT)
    pct_text = str(int(session_pct))
    font_size = 8.0 if session_pct < 100 else 6.5
    ctx.set_font_size(font_size)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    extents = ctx.text_extents(pct_text)
    ctx.move_to(center - extents.width / 2 - extents.x_bearing,
                center - extents.height / 2 - extents.y_bearing)
    ctx.show_text(pct_text)

    # Convert cairo surface to GdkPixbuf
    surface.flush()
    data = bytes(surface.get_data())
    # Cairo uses BGRA, GdkPixbuf uses RGBA - swap channels
    rgba_data = bytearray(len(data))
    for i in range(0, len(data), 4):
        rgba_data[i] = data[i + 2]      # R <- B
        rgba_data[i + 1] = data[i + 1]  # G
        rgba_data[i + 2] = data[i + 0]  # B <- R
        rgba_data[i + 3] = data[i + 3]  # A

    pixbuf = GdkPixbuf.Pixbuf.new_from_data(
        bytes(rgba_data), GdkPixbuf.Colorspace.RGB, True, 8,
        size, size, size * 4
    )
    return pixbuf


# --- API Client ---

class ClaudeAPIClient:
    """Fetches usage data from the Claude API using CLI OAuth credentials."""

    def __init__(self):
        self.access_token = None
        self.expires_at = 0
        self.subscription_type = None
        self.rate_limit_tier = None
        self._backoff_until = 0
        self._claude_version = self._get_claude_version()
        self._load_credentials()

    def _load_credentials(self):
        """Load OAuth token from Claude Code credentials file."""
        try:
            with open(CREDENTIALS_PATH, "r") as f:
                creds = json.load(f)
            oauth = creds.get("claudeAiOauth", {})
            self.access_token = oauth.get("accessToken")
            self.expires_at = oauth.get("expiresAt", 0)
            self.subscription_type = oauth.get("subscriptionType", "unknown")
            self.rate_limit_tier = oauth.get("rateLimitTier", "unknown")
            if not self.access_token:
                print(f"[ERROR] No accessToken found in {CREDENTIALS_PATH}")
        except FileNotFoundError:
            print(f"[ERROR] Credentials file not found: {CREDENTIALS_PATH}")
            print("Make sure Claude Code CLI is logged in.")
        except json.JSONDecodeError as e:
            print(f"[ERROR] Failed to parse credentials: {e}")

    def reload_credentials(self):
        """Reload credentials from disk (in case of token refresh)."""
        self._load_credentials()

    def _refresh_token(self):
        """Refresh the OAuth token to get a new access token with fresh rate limits.

        Each access token has a very low rate limit (~5 requests) on the usage
        endpoint. Refreshing gives us a new token with a fresh budget.
        Note: refresh tokens are single-use, so we must save the new one.
        """
        try:
            with open(CREDENTIALS_PATH, "r") as f:
                creds = json.load(f)
            oauth = creds.get("claudeAiOauth", {})
            refresh_token = oauth.get("refreshToken")
            if not refresh_token:
                return False

            resp = requests.post(
                "https://console.anthropic.com/v1/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                },
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return False

            data = resp.json()
            new_access = data.get("access_token")
            new_refresh = data.get("refresh_token")
            if not new_access or not new_refresh:
                return False

            # Update credentials file
            oauth["accessToken"] = new_access
            oauth["refreshToken"] = new_refresh
            oauth["expiresAt"] = int(time.time() * 1000) + data.get("expires_in", 3600) * 1000
            creds["claudeAiOauth"] = oauth
            with open(CREDENTIALS_PATH, "w") as f:
                json.dump(creds, f, indent=2)

            # Update in-memory state
            self.access_token = new_access
            self.expires_at = oauth["expiresAt"]
            return True
        except Exception:
            return False

    def is_token_expired(self):
        """Check if the OAuth token has expired."""
        if not self.expires_at:
            return True
        # expiresAt is in milliseconds
        return time.time() * 1000 >= self.expires_at

    @staticmethod
    def _get_claude_version():
        """Get installed Claude Code version."""
        try:
            result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip().split()[0]
        except Exception:
            pass
        return "2.1.5"

    def _build_headers(self):
        """Build authenticated request headers."""
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": f"claude-code/{self._claude_version}",
            "anthropic-beta": "oauth-2025-04-20",
        }

    def _ensure_token(self):
        """Ensure we have a valid token. Returns error dict or None."""
        if not self.access_token:
            self.reload_credentials()
            if not self.access_token:
                return {"error": "No credentials. Run: claude login"}
        if self.is_token_expired():
            self.reload_credentials()
            if self.is_token_expired():
                return {"error": "Token expired. Re-login with: claude login"}
        return None

    def fetch_usage(self):
        """Fetch usage data from the API. Returns dict or None on error."""
        if time.time() < self._backoff_until:
            remaining = int(self._backoff_until - time.time())
            return {"error": f"Rate limited, retrying in {remaining}s"}
        err = self._ensure_token()
        if err:
            return err

        try:
            resp = requests.get(USAGE_API_URL, headers=self._build_headers(), timeout=15)
            if resp.status_code == 401:
                self.reload_credentials()
                return {"error": "Authentication failed (401). Try: claude login"}
            if resp.status_code == 403:
                return {"error": "Access forbidden (403). Check your plan."}
            if resp.status_code == 429:
                # Try refreshing the token for a fresh rate limit budget
                if self._refresh_token():
                    # Retry once with new token
                    resp = requests.get(USAGE_API_URL, headers=self._build_headers(), timeout=15)
                    if resp.status_code == 200:
                        return resp.json()
                # Still failing — back off
                self._backoff_until = time.time() + 180
                return {"error": "Rate limited (429). Backing off 180s"}
            resp.raise_for_status()
            return resp.json()
        except requests.ConnectionError:
            return {"error": "Network error - check your connection"}
        except requests.Timeout:
            return {"error": "Request timed out"}
        except requests.RequestException as e:
            return {"error": f"API error: {e}"}
        except json.JSONDecodeError:
            return {"error": "Invalid JSON response from API"}

    def refresh_plan_info(self):
        """Fetch current plan info from the profile API.

        Updates rate_limit_tier and subscription_type from the server
        so plan changes are detected without re-login.
        """
        err = self._ensure_token()
        if err:
            return
        try:
            resp = requests.get(PROFILE_API_URL, headers=self._build_headers(), timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                org = data.get("organization", {})
                tier = org.get("rate_limit_tier")
                org_type = org.get("organization_type")
                if tier:
                    self.rate_limit_tier = tier
                if org_type:
                    self.subscription_type = org_type
        except Exception:
            pass  # Non-critical, keep using cached values

    def fetch_models(self):
        """Fetch available models from the account API.

        Returns list of dicts with name, description, active status.
        """
        err = self._ensure_token()
        if err:
            return []
        try:
            resp = requests.get(ACCOUNT_API_URL, headers=self._build_headers(), timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            memberships = data.get("memberships", [])
            # Find the claude_max / chat org
            for m in memberships:
                org = m.get("organization", {})
                caps = org.get("capabilities", [])
                if "chat" in caps or "claude_max" in caps:
                    return org.get("claude_ai_bootstrap_models_config", [])
            return []
        except Exception:
            return []

    @staticmethod
    def fetch_status():
        """Fetch Claude system status. Returns (indicator, description)."""
        try:
            resp = requests.get(STATUS_API_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", {})
                return (
                    status.get("indicator", "unknown"),
                    status.get("description", "Unknown"),
                )
        except Exception:
            pass
        return ("unknown", "Unable to check status")


# --- Usage Data Model ---

class UsageData:
    """Parsed usage data with convenience properties."""

    def __init__(self, raw=None):
        self.raw = raw or {}
        self.error = raw.get("error") if raw else "No data"
        self.last_updated = datetime.now()

    @property
    def ok(self):
        return self.error is None and self.raw

    def _get_utilization(self, key):
        entry = self.raw.get(key, {})
        if not entry:
            return 0.0
        val = entry.get("utilization", 0)
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    def _get_reset(self, key):
        entry = self.raw.get(key, {})
        if not entry:
            return None
        return entry.get("resets_at")

    @property
    def session_pct(self):
        return self._get_utilization("five_hour")

    @property
    def session_reset(self):
        return self._get_reset("five_hour")

    @property
    def weekly_pct(self):
        return self._get_utilization("seven_day")

    @property
    def weekly_reset(self):
        return self._get_reset("seven_day")

    @property
    def opus_pct(self):
        return self._get_utilization("seven_day_opus")

    @property
    def opus_reset(self):
        return self._get_reset("seven_day_opus")

    @property
    def sonnet_pct(self):
        return self._get_utilization("seven_day_sonnet")

    @property
    def sonnet_reset(self):
        return self._get_reset("seven_day_sonnet")

    @property
    def extra_usage(self):
        """Return extra_usage dict if enabled, else None."""
        extra = self.raw.get("extra_usage")
        if extra and extra.get("is_enabled"):
            return extra
        return None


# --- Popup Window ---

class UsagePopup(Gtk.Window):
    """A popup window showing detailed usage statistics."""

    def __init__(self, usage_data, client, on_refresh, models=None, status_indicator=None, status_desc=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_resizable(False)
        self.set_border_width(0)
        self.on_refresh = on_refresh

        # Close on focus loss
        self.connect("focus-out-event", lambda w, e: w.destroy())

        # Style the window
        css = Gtk.CssProvider()
        css.load_from_data(self._get_css().encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Build content
        frame = Gtk.Frame()
        frame.get_style_context().add_class("popup-frame")

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.get_style_context().add_class("popup-main")

        # Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.get_style_context().add_class("popup-header")
        header_label = Gtk.Label(label="Claude Usage")
        header_label.get_style_context().add_class("header-title")
        header_label.set_halign(Gtk.Align.START)

        plan_label = Gtk.Label(label=client.rate_limit_tier.replace("default_claude_", "").replace("_", " ").title() if client.rate_limit_tier else "Unknown")
        plan_label.get_style_context().add_class("plan-badge")

        header_box.pack_start(header_label, True, True, 0)
        header_box.pack_end(plan_label, False, False, 0)
        main_box.pack_start(header_box, False, False, 0)

        if not usage_data.ok:
            # Error state
            error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            error_box.set_margin_top(16)
            error_box.set_margin_bottom(16)
            error_box.set_margin_start(16)
            error_box.set_margin_end(16)
            error_label = Gtk.Label(label=usage_data.error or "Failed to load data")
            error_label.get_style_context().add_class("error-text")
            error_label.set_line_wrap(True)
            error_label.set_max_width_chars(35)
            error_box.pack_start(error_label, False, False, 0)
            main_box.pack_start(error_box, False, False, 0)
        else:
            # Usage bars
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content_box.set_margin_top(12)
            content_box.set_margin_bottom(8)
            content_box.set_margin_start(16)
            content_box.set_margin_end(16)

            content_box.pack_start(
                self._make_usage_row("Session (5h)", usage_data.session_pct, usage_data.session_reset),
                False, False, 0
            )
            # Session pacing
            session_pace = calc_pacing(usage_data.session_pct, usage_data.session_reset, 5)
            if session_pace:
                content_box.pack_start(self._make_pace_row(session_pace), False, False, 0)

            content_box.pack_start(
                self._make_usage_row("Weekly", usage_data.weekly_pct, usage_data.weekly_reset),
                False, False, 0
            )
            # Weekly pacing
            weekly_pace = calc_pacing(usage_data.weekly_pct, usage_data.weekly_reset, 168)
            if weekly_pace:
                content_box.pack_start(self._make_pace_row(weekly_pace), False, False, 0)

            if usage_data.opus_pct > 0 or usage_data.raw.get("seven_day_opus"):
                content_box.pack_start(
                    self._make_usage_row("Opus (weekly)", usage_data.opus_pct, usage_data.opus_reset),
                    False, False, 0
                )
            if usage_data.sonnet_pct > 0 or usage_data.raw.get("seven_day_sonnet"):
                content_box.pack_start(
                    self._make_usage_row("Sonnet (weekly)", usage_data.sonnet_pct, usage_data.sonnet_reset),
                    False, False, 0
                )

            main_box.pack_start(content_box, False, False, 0)

            # Extra usage on/off indicator (like status bar)
            extra_raw = usage_data.raw.get("extra_usage")
            is_enabled = extra_raw.get("is_enabled", False) if extra_raw else False
            extra_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            extra_box.get_style_context().add_class("status-bar")

            extra_dot = Gtk.Label()
            if is_enabled:
                extra_dot.set_markup('<span foreground="#4CAF50">●</span>')
                used_cents = (extra_raw.get("used_credits", 0) or 0)
                limit_cents = (extra_raw.get("monthly_limit", 0) or 0)
                used = used_cents / 100.0
                limit = limit_cents / 100.0
                status_text = f"Extra Usage On  ({used:.2f} / {limit:.2f} used)"
            else:
                extra_dot.set_markup('<span foreground="#F44336">●</span>')
                status_text = "Extra Usage Off"

            extra_label = Gtk.Label(label=status_text)
            extra_label.get_style_context().add_class("status-text")
            extra_label.set_halign(Gtk.Align.START)

            extra_box.pack_start(extra_dot, False, False, 0)
            extra_box.pack_start(extra_label, True, True, 0)
            main_box.pack_start(extra_box, False, False, 0)

        # System status
        if status_indicator:
            status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            status_box.get_style_context().add_class("status-bar")

            dot_label = Gtk.Label()
            if status_indicator == "none":
                dot_label.set_markup('<span foreground="#4CAF50">●</span>')
            elif status_indicator == "minor":
                dot_label.set_markup('<span foreground="#FFC107">●</span>')
            elif status_indicator in ("major", "critical"):
                dot_label.set_markup('<span foreground="#F44336">●</span>')
            else:
                dot_label.set_markup('<span foreground="#777">●</span>')

            status_text = Gtk.Label(label=status_desc or "Unknown")
            status_text.get_style_context().add_class("status-text")
            status_text.set_halign(Gtk.Align.START)

            status_box.pack_start(dot_label, False, False, 0)
            status_box.pack_start(status_text, True, True, 0)
            main_box.pack_start(status_box, False, False, 0)

        # Available models
        if models:
            models_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            models_box.get_style_context().add_class("models-section")

            models_header = Gtk.Label(label="Available Models")
            models_header.get_style_context().add_class("models-header")
            models_header.set_halign(Gtk.Align.START)
            models_box.pack_start(models_header, False, False, 0)

            for model in models:
                if model.get("inactive"):
                    continue
                name = model.get("name", model.get("model", "Unknown"))
                desc = (model.get("description") or "").strip()
                overflow = model.get("overflow", False)

                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                name_label = Gtk.Label(label=name)
                name_label.get_style_context().add_class("model-name")
                name_label.set_halign(Gtk.Align.START)
                row.pack_start(name_label, False, False, 0)

                if desc:
                    desc_label = Gtk.Label(label=desc)
                    desc_label.get_style_context().add_class("model-desc")
                    row.pack_start(desc_label, False, False, 0)

                if overflow:
                    overflow_label = Gtk.Label(label="older")
                    overflow_label.get_style_context().add_class("model-overflow")
                    row.pack_end(overflow_label, False, False, 0)

                models_box.pack_start(row, False, False, 0)

            main_box.pack_start(models_box, False, False, 0)

        # Footer
        footer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer_box.get_style_context().add_class("popup-footer")

        updated_label = Gtk.Label(
            label=f"Updated: {usage_data.last_updated.strftime('%H:%M:%S')}"
        )
        updated_label.get_style_context().add_class("footer-text")
        updated_label.set_halign(Gtk.Align.START)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.get_style_context().add_class("refresh-btn")
        refresh_btn.connect("clicked", self._on_refresh_clicked)

        footer_box.pack_start(updated_label, True, True, 0)
        footer_box.pack_end(refresh_btn, False, False, 0)
        main_box.pack_start(footer_box, False, False, 0)

        frame.add(main_box)
        self.add(frame)
        self.show_all()

    def _on_refresh_clicked(self, button):
        self.destroy()
        if self.on_refresh:
            self.on_refresh()

    def _make_usage_row(self, label_text, pct, reset_time):
        """Create a labeled usage bar with percentage and countdown."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        # Top row: label + percentage
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        label = Gtk.Label(label=label_text)
        label.get_style_context().add_class("usage-label")
        label.set_halign(Gtk.Align.START)

        pct_label = Gtk.Label(label=f"{pct:.1f}%")
        pct_label.get_style_context().add_class("usage-pct")
        if pct >= 90:
            pct_label.get_style_context().add_class("pct-red")
        elif pct >= 75:
            pct_label.get_style_context().add_class("pct-yellow")
        else:
            pct_label.get_style_context().add_class("pct-green")

        top_row.pack_start(label, True, True, 0)
        top_row.pack_end(pct_label, False, False, 0)
        box.pack_start(top_row, False, False, 0)

        # Progress bar
        bar = Gtk.ProgressBar()
        bar.set_fraction(min(pct / 100.0, 1.0))
        bar.get_style_context().add_class("usage-bar")
        if pct >= 90:
            bar.get_style_context().add_class("bar-red")
        elif pct >= 75:
            bar.get_style_context().add_class("bar-yellow")
        else:
            bar.get_style_context().add_class("bar-green")
        box.pack_start(bar, False, False, 0)

        # Reset countdown
        if reset_time:
            reset_label = Gtk.Label(label=f"Resets in {format_countdown(reset_time)}  ({format_time(reset_time)})")
            reset_label.get_style_context().add_class("reset-text")
            reset_label.set_halign(Gtk.Align.START)
            box.pack_start(reset_label, False, False, 0)

        return box

    def _make_pace_row(self, pacing):
        """Create a pacing indicator row from calc_pacing() output."""
        elapsed, total, unit, expected_pct, pace_diff = pacing
        pace_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        pace_box.set_margin_start(4)

        unit_label = "Hour" if unit == "h" else "Day"
        time_label = Gtk.Label(label=f"{unit_label} {elapsed:.1f}/{total:.0f}")
        time_label.get_style_context().add_class("reset-text")
        time_label.set_halign(Gtk.Align.START)

        if pace_diff > PACE_FIRST_THRESHOLD:
            pace_text = f"  {pace_diff:+.0f}% ahead"
            pace_class = "pct-red"
        elif pace_diff > 5:
            pace_text = f"  {pace_diff:+.0f}% ahead"
            pace_class = "pct-yellow"
        elif pace_diff < -5:
            pace_text = f"  {abs(pace_diff):.0f}% under"
            pace_class = "pct-green"
        else:
            pace_text = "  On pace"
            pace_class = "pct-green"

        pace_label = Gtk.Label(label=pace_text)
        pace_label.get_style_context().add_class("usage-pct")
        pace_label.get_style_context().add_class(pace_class)

        expected_label = Gtk.Label(label=f"  (expected ~{expected_pct:.0f}%)")
        expected_label.get_style_context().add_class("reset-text")

        pace_box.pack_start(time_label, False, False, 0)
        pace_box.pack_start(pace_label, False, False, 0)
        pace_box.pack_start(expected_label, False, False, 0)
        return pace_box

    def _get_css(self):
        return """
        .popup-frame {
            border: 1px solid #555;
            border-radius: 8px;
            background: transparent;
        }
        .popup-main {
            background: #1e1e1e;
            border-radius: 8px;
            min-width: 320px;
        }
        .popup-header {
            padding: 12px 16px;
            background: #2a2a2a;
            border-radius: 8px 8px 0 0;
        }
        .header-title {
            font-size: 14px;
            font-weight: bold;
            color: #e0e0e0;
        }
        .plan-badge {
            font-size: 11px;
            color: #90CAF9;
            background: #1a237e;
            padding: 2px 8px;
            border-radius: 4px;
        }
        .usage-label {
            font-size: 12px;
            color: #b0b0b0;
        }
        .usage-pct {
            font-size: 12px;
            font-weight: bold;
        }
        .pct-green { color: #4CAF50; }
        .pct-yellow { color: #FFC107; }
        .pct-red { color: #F44336; }

        .usage-bar {
            min-height: 6px;
            border-radius: 3px;
        }
        .usage-bar trough {
            min-height: 6px;
            border-radius: 3px;
            background: #333;
        }
        .bar-green progress { background: #4CAF50; border-radius: 3px; }
        .bar-yellow progress { background: #FFC107; border-radius: 3px; }
        .bar-red progress { background: #F44336; border-radius: 3px; }

        .reset-text {
            font-size: 10px;
            color: #777;
        }
        .error-text {
            font-size: 12px;
            color: #F44336;
        }
        .popup-footer {
            padding: 8px 16px;
            background: #252525;
            border-radius: 0 0 8px 8px;
        }
        .footer-text {
            font-size: 10px;
            color: #666;
        }
        .refresh-btn {
            font-size: 11px;
            padding: 2px 12px;
            border-radius: 4px;
            background: #333;
            color: #ccc;
            border: 1px solid #555;
        }
        .refresh-btn:hover {
            background: #444;
        }
        .status-bar {
            padding: 6px 16px;
            background: #232323;
        }
        .status-text {
            font-size: 11px;
            color: #b0b0b0;
        }
        .models-section {
            padding: 8px 16px;
            background: #1e1e1e;
        }
        .models-header {
            font-size: 11px;
            font-weight: bold;
            color: #888;
            margin-bottom: 2px;
        }
        .model-name {
            font-size: 11px;
            color: #ccc;
        }
        .model-desc {
            font-size: 10px;
            color: #666;
        }
        .model-overflow {
            font-size: 9px;
            color: #555;
            background: #2a2a2a;
            padding: 0px 4px;
            border-radius: 3px;
        }
        """

    def position_near(self, x, y, panel_position=None):
        """Position the popup near the tray icon coordinates."""
        self.realize()
        alloc = self.get_allocation()
        width = alloc.width if alloc.width > 1 else 340
        height = alloc.height if alloc.height > 1 else 300

        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        workarea = monitor.get_workarea()

        px = x - width // 2

        # If click is in top half of screen (top panel), position below
        # If click is in bottom half (bottom panel), position above
        if y < workarea.y + workarea.height // 2:
            py = y + 8  # Below the panel
        else:
            py = y - height - 8  # Above the panel

        # Clamp to workarea
        px = max(workarea.x, min(px, workarea.x + workarea.width - width))
        py = max(workarea.y, min(py, workarea.y + workarea.height - height))

        self.move(px, py)


# --- Main Application ---

class ClaudeUsageTracker:
    """Main application class."""

    def __init__(self):
        self.client = ClaudeAPIClient()
        self.usage = UsageData()
        self.popup = None
        self.models = []
        self.status_indicator = "none"
        self.status_desc = "Checking..."
        self._notified_session = set()
        self._notified_weekly = set()
        self._notified_session_pace = set()
        self._notified_weekly_pace = set()
        self._last_session_reset = None
        self._last_weekly_reset = None
        self._poll_count = 0
        self._last_fetch_time = 0

        # Initialize notifications
        Notify.init(APP_ID)

        # Create tray icon
        self.icon = XApp.StatusIcon()
        self.icon.set_name(APP_ID)
        self.icon.set_tooltip_text("Claude Usage Tracker - Loading...")
        self.icon.set_visible(True)

        # Set initial icon
        self._update_icon(0)

        # Right-click menu
        menu = Gtk.Menu()
        item_refresh = Gtk.MenuItem.new_with_label("Refresh Now")
        item_refresh.connect("activate", lambda _: self._trigger_refresh(force=True))
        menu.append(item_refresh)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem.new_with_label("Quit")
        item_quit.connect("activate", lambda _: self._quit())
        menu.append(item_quit)

        menu.show_all()
        self.icon.set_secondary_menu(menu)

        # Left-click: show popup
        self.icon.connect("activate", self._on_activate)

        # Button press for positioning
        self._last_click_x = 0
        self._last_click_y = 0
        self.icon.connect("button-press-event", self._on_button_press)

        # Initial fetch
        GLib.timeout_add(500, self._initial_fetch)

        # Periodic polling
        GLib.timeout_add_seconds(POLL_INTERVAL_SECONDS, self._poll)

    def _update_icon(self, session_pct):
        """Update the tray icon pixbuf."""
        pixbuf = render_icon(session_pct)
        # Use a rotating filename so XApp detects the change
        if not hasattr(self, '_icon_counter'):
            self._icon_counter = 0
        self._icon_counter = (self._icon_counter + 1) % 2
        icon_path = f"/tmp/claude-usage-icon-{self._icon_counter}.png"
        # Remove old file to avoid stale cache
        old_path = f"/tmp/claude-usage-icon-{(self._icon_counter + 1) % 2}.png"
        try:
            os.remove(old_path)
        except FileNotFoundError:
            pass
        pixbuf.savev(icon_path, "png", [], [])
        self.icon.set_icon_name(icon_path)

    def _on_button_press(self, icon, x, y, button, time, panel_position):
        """Record click position for popup placement."""
        self._last_click_x = x
        self._last_click_y = y
        self._panel_position = panel_position

    def _on_activate(self, icon, button, time):
        """Handle left-click: toggle popup."""
        if self.popup and self.popup.get_visible():
            self.popup.destroy()
            self.popup = None
            return

        self.popup = UsagePopup(
            self.usage, self.client, lambda: self._trigger_refresh(force=True),
            models=self.models,
            status_indicator=self.status_indicator,
            status_desc=self.status_desc,
        )
        self.popup.position_near(self._last_click_x, self._last_click_y, getattr(self, '_panel_position', None))

    def _trigger_refresh(self, force=False):
        """Trigger an immediate refresh in a background thread."""
        thread = threading.Thread(target=self._fetch_and_update, args=(force,), daemon=True)
        thread.start()

    def _initial_fetch(self):
        """Initial data fetch (called once after startup)."""
        self._trigger_refresh()
        return False  # Don't repeat

    def _poll(self):
        """Periodic polling callback."""
        self._trigger_refresh()
        return True  # Keep repeating

    def _fetch_and_update(self, force=False):
        """Fetch usage data and update UI (runs in background thread)."""
        # Guard against burst calls after suspend/resume (skip on manual refresh)
        now = time.time()
        if not force and now - self._last_fetch_time < 30:
            return
        self._last_fetch_time = now
        if force:
            # Manual refresh: reload credentials from disk and clear backoff
            self.client.reload_credentials()
            self.client._backoff_until = 0
        self._poll_count += 1
        # Refresh plan info and models every 10 polls (~10 min) to reduce API calls
        if self._poll_count % 10 == 1:
            self.client.refresh_plan_info()
        raw = self.client.fetch_usage()
        usage = UsageData(raw)
        # Write successful usage data to shared cache for statusline
        if usage.ok:
            try:
                cache_data = dict(raw)
                cache_data["_ts"] = time.time()
                STATUSLINE_CACHE_PATH.write_text(json.dumps(cache_data))
            except OSError:
                pass
        models = self.client.fetch_models() if self._poll_count % 10 == 1 else self.models
        status_indicator, status_desc = self.client.fetch_status()
        # Schedule UI update on main thread
        GLib.idle_add(self._apply_update, usage, models, status_indicator, status_desc)

    def _apply_update(self, usage, models=None, status_indicator=None, status_desc=None):
        """Apply fetched data to UI (must run on main thread)."""
        self.usage = usage
        if models is not None:
            self.models = models
        if status_indicator is not None:
            self.status_indicator = status_indicator
            self.status_desc = status_desc

        if usage.ok:
            self._update_icon(usage.session_pct)
            status_line = ""
            if self.status_indicator and self.status_indicator != "none":
                status_line = f"\nStatus: {self.status_desc}"
            self.icon.set_tooltip_text(
                f"Session: {usage.session_pct:.1f}% | "
                f"Weekly: {usage.weekly_pct:.1f}%\n"
                f"Session resets: {format_countdown(usage.session_reset)}"
                f"{status_line}"
            )
            self._check_notifications(usage)
        else:
            self._update_icon(0)
            self.icon.set_tooltip_text(f"Claude Usage - {usage.error}")

        return False  # Don't repeat idle_add

    def _check_notifications(self, usage):
        """Send desktop notifications at usage thresholds."""
        # Normalize reset times to the minute so slight API variations
        # don't clear the notification tracking sets
        norm_session = normalize_reset_time(usage.session_reset)
        norm_weekly = normalize_reset_time(usage.weekly_reset)

        if norm_session != self._last_session_reset:
            self._notified_session.clear()
            self._notified_session_pace.clear()
            self._last_session_reset = norm_session
        if norm_weekly != self._last_weekly_reset:
            self._notified_weekly.clear()
            self._notified_weekly_pace.clear()
            self._last_weekly_reset = norm_weekly

        # Threshold notifications: every 5% from 75 onwards, each fires once
        for threshold in NOTIFY_THRESHOLDS:
            if usage.session_pct >= threshold and threshold not in self._notified_session:
                self._notified_session.add(threshold)
                self._send_notification(
                    f"Session usage at {usage.session_pct:.0f}%",
                    f"5-hour session usage has reached {threshold}%. "
                    f"Resets in {format_countdown(usage.session_reset)}.",
                    "dialog-warning" if threshold >= 90 else "dialog-information"
                )
            if usage.weekly_pct >= threshold and threshold not in self._notified_weekly:
                self._notified_weekly.add(threshold)
                self._send_notification(
                    f"Weekly usage at {usage.weekly_pct:.0f}%",
                    f"7-day weekly usage has reached {threshold}%. "
                    f"Resets in {format_countdown(usage.weekly_reset)}.",
                    "dialog-warning" if threshold >= 90 else "dialog-information"
                )

        # Pacing alerts for session and weekly
        self._check_pace_notifications(
            "Session", usage.session_pct, usage.session_reset,
            5, self._notified_session_pace
        )
        self._check_pace_notifications(
            "Weekly", usage.weekly_pct, usage.weekly_reset,
            168, self._notified_weekly_pace
        )

    def _check_pace_notifications(self, label, pct, reset_iso, window_hours, notified_set):
        """Check and send pacing notifications for a usage window."""
        pacing = calc_pacing(pct, reset_iso, window_hours)
        if not pacing:
            return

        elapsed, total, unit, expected_pct, pace_diff = pacing

        # Grace period: skip alerts in the first 10 minutes of a new window
        elapsed_minutes = elapsed * 60 if unit == "h" else elapsed * 24 * 60
        if elapsed_minutes < PACE_GRACE_MINUTES:
            return

        if pace_diff < PACE_FIRST_THRESHOLD:
            return

        # Build thresholds: first at PACE_FIRST_THRESHOLD, then every PACE_STEP
        # e.g. 10, 15, 20, 25, ...
        threshold = PACE_FIRST_THRESHOLD
        while threshold <= pace_diff:
            if threshold not in notified_set:
                notified_set.add(threshold)
                unit_label = "Hour" if unit == "h" else "Day"
                self._send_notification(
                    f"{label} usage {pace_diff:.0f}% ahead of pace",
                    f"{unit_label} {elapsed:.1f}/{total:.0f}: using {pct:.0f}% "
                    f"(expected ~{expected_pct:.0f}%). "
                    f"You may run out before reset.",
                    "dialog-warning"
                )
            threshold += PACE_STEP

    def _send_notification(self, title, body, icon_name):
        """Send a desktop notification."""
        try:
            notif = Notify.Notification.new(title, body, icon_name)
            notif.set_app_name(APP_NAME)
            notif.set_urgency(Notify.Urgency.NORMAL)
            notif.show()
        except Exception as e:
            print(f"[WARN] Notification failed: {e}")

    def _quit(self):
        """Clean up and exit."""
        self.icon.set_visible(False)
        Notify.uninit()
        Gtk.main_quit()

    def run(self):
        """Run the application main loop."""
        # Handle SIGINT/SIGTERM gracefully
        signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(self._quit))
        signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(self._quit))
        # GLib needs to be aware of signals
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)

        Gtk.main()


def main():
    app = ClaudeUsageTracker()
    app.run()


if __name__ == "__main__":
    main()
