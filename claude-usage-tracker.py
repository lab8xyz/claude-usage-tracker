#!/usr/bin/env python3
"""Claude Usage Tracker - Linux Mint system tray application.

Monitors Claude AI usage limits in real-time via the system tray.
Reads OAuth credentials from Claude Code CLI (~/.claude/.credentials.json).
"""

import json
import math
import os
import signal
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
POLL_INTERVAL_SECONDS = 60
ICON_SIZE = 24
APP_ID = "claude-usage-tracker"
APP_NAME = "Claude Usage Tracker"

# Notification thresholds: every 5% from 75 onwards, each fires once per reset cycle
NOTIFY_THRESHOLDS = list(range(75, 101, 5))  # [75, 80, 85, 90, 95, 100]

# Daily pacing: alert if weekly usage exceeds expected pace by this many percentage points
PACE_ALERT_MARGIN = 15

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
        return local_time.strftime("%b %d, %I:%M %p")
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


def calc_weekly_pacing(weekly_pct, weekly_reset_iso):
    """Calculate daily pacing for weekly usage.

    Returns (days_elapsed, days_total, expected_pct, pace_diff) or None.
    - days_elapsed: how many days into the 7-day window
    - expected_pct: what usage % you'd ideally be at for even daily use
    - pace_diff: actual - expected (positive = ahead/burning fast)
    """
    if not weekly_reset_iso:
        return None
    try:
        reset_time = datetime.fromisoformat(weekly_reset_iso)
        now = datetime.now(timezone.utc)
        remaining = reset_time - now
        hours_remaining = max(remaining.total_seconds() / 3600, 0)
        days_total = 7.0
        days_remaining = hours_remaining / 24.0
        days_elapsed = days_total - days_remaining
        if days_elapsed < 0:
            days_elapsed = 0
        expected_pct = (days_elapsed / days_total) * 100
        pace_diff = weekly_pct - expected_pct
        return (days_elapsed, days_total, expected_pct, pace_diff)
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

    def is_token_expired(self):
        """Check if the OAuth token has expired."""
        if not self.expires_at:
            return True
        # expiresAt is in milliseconds
        return time.time() * 1000 >= self.expires_at

    def fetch_usage(self):
        """Fetch usage data from the API. Returns dict or None on error."""
        if not self.access_token:
            self.reload_credentials()
            if not self.access_token:
                return None

        if self.is_token_expired():
            self.reload_credentials()
            if self.is_token_expired():
                return {"error": "Token expired. Re-login with: claude login"}

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "claude-code/2.1.5",
            "anthropic-beta": "oauth-2025-04-20",
        }
        try:
            resp = requests.get(USAGE_API_URL, headers=headers, timeout=15)
            if resp.status_code == 401:
                self.reload_credentials()
                return {"error": "Authentication failed (401). Try: claude login"}
            if resp.status_code == 403:
                return {"error": "Access forbidden (403). Check your plan."}
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

    def __init__(self, usage_data, client, on_refresh):
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
            content_box.pack_start(
                self._make_usage_row("Weekly", usage_data.weekly_pct, usage_data.weekly_reset),
                False, False, 0
            )

            # Weekly pacing indicator
            pacing = calc_weekly_pacing(usage_data.weekly_pct, usage_data.weekly_reset)
            if pacing:
                days_elapsed, days_total, expected_pct, pace_diff = pacing
                pace_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                pace_box.set_margin_start(4)

                day_label = Gtk.Label(label=f"Day {days_elapsed:.1f}/7")
                day_label.get_style_context().add_class("reset-text")
                day_label.set_halign(Gtk.Align.START)

                if pace_diff > PACE_ALERT_MARGIN:
                    pace_text = f"  {pace_diff:+.0f}% ahead of pace"
                    pace_class = "pct-red"
                elif pace_diff > 5:
                    pace_text = f"  {pace_diff:+.0f}% ahead of pace"
                    pace_class = "pct-yellow"
                elif pace_diff < -5:
                    pace_text = f"  {abs(pace_diff):.0f}% under pace"
                    pace_class = "pct-green"
                else:
                    pace_text = "  On pace"
                    pace_class = "pct-green"

                pace_label = Gtk.Label(label=pace_text)
                pace_label.get_style_context().add_class("usage-pct")
                pace_label.get_style_context().add_class(pace_class)

                expected_label = Gtk.Label(label=f"  (expected ~{expected_pct:.0f}%)")
                expected_label.get_style_context().add_class("reset-text")

                pace_box.pack_start(day_label, False, False, 0)
                pace_box.pack_start(pace_label, False, False, 0)
                pace_box.pack_start(expected_label, False, False, 0)
                content_box.pack_start(pace_box, False, False, 0)

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

            # Extra usage / overage credits
            extra = usage_data.extra_usage
            if extra:
                sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                sep.set_margin_top(4)
                content_box.pack_start(sep, False, False, 0)
                extra_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                extra_label = Gtk.Label(label="Overage Credits")
                extra_label.get_style_context().add_class("usage-label")
                extra_label.set_halign(Gtk.Align.START)
                used = extra.get("used_credits", 0) or 0
                limit = extra.get("monthly_limit", 0) or 0
                extra_val = Gtk.Label(label=f"${used:.2f} / ${limit:.2f}")
                extra_val.get_style_context().add_class("usage-pct")
                extra_val.get_style_context().add_class("pct-green")
                extra_box.pack_start(extra_label, True, True, 0)
                extra_box.pack_end(extra_val, False, False, 0)
                content_box.pack_start(extra_box, False, False, 0)

            main_box.pack_start(content_box, False, False, 0)

        # Footer
        footer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer_box.get_style_context().add_class("popup-footer")

        updated_label = Gtk.Label(
            label=f"Updated: {usage_data.last_updated.strftime('%I:%M:%S %p')}"
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
        self._notified_session = set()
        self._notified_weekly = set()
        self._notified_pacing = False
        self._last_session_reset = None
        self._last_weekly_reset = None

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
        item_refresh.connect("activate", lambda _: self._trigger_refresh())
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

        self.popup = UsagePopup(self.usage, self.client, self._trigger_refresh)
        self.popup.position_near(self._last_click_x, self._last_click_y, getattr(self, '_panel_position', None))

    def _trigger_refresh(self):
        """Trigger an immediate refresh in a background thread."""
        thread = threading.Thread(target=self._fetch_and_update, daemon=True)
        thread.start()

    def _initial_fetch(self):
        """Initial data fetch (called once after startup)."""
        self._trigger_refresh()
        return False  # Don't repeat

    def _poll(self):
        """Periodic polling callback."""
        self._trigger_refresh()
        return True  # Keep repeating

    def _fetch_and_update(self):
        """Fetch usage data and update UI (runs in background thread)."""
        raw = self.client.fetch_usage()
        usage = UsageData(raw)
        # Schedule UI update on main thread
        GLib.idle_add(self._apply_update, usage)

    def _apply_update(self, usage):
        """Apply fetched data to UI (must run on main thread)."""
        self.usage = usage

        if usage.ok:
            self._update_icon(usage.session_pct)
            self.icon.set_tooltip_text(
                f"Session: {usage.session_pct:.1f}% | "
                f"Weekly: {usage.weekly_pct:.1f}%\n"
                f"Session resets: {format_countdown(usage.session_reset)}"
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
            self._last_session_reset = norm_session
        if norm_weekly != self._last_weekly_reset:
            self._notified_weekly.clear()
            self._notified_pacing = False
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

        # Daily pacing alert: notify once when significantly ahead of pace
        if not self._notified_pacing:
            pacing = calc_weekly_pacing(usage.weekly_pct, usage.weekly_reset)
            if pacing:
                days_elapsed, _, expected_pct, pace_diff = pacing
                if pace_diff > PACE_ALERT_MARGIN:
                    self._notified_pacing = True
                    self._send_notification(
                        f"Weekly usage ahead of pace",
                        f"Day {days_elapsed:.1f}/7: using {usage.weekly_pct:.0f}% "
                        f"(expected ~{expected_pct:.0f}%). "
                        f"{pace_diff:.0f}% ahead - you may run out before reset.",
                        "dialog-warning"
                    )

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
