# Claude Usage Tracker

A lightweight Linux Mint system tray application that monitors your Claude AI usage limits in real-time.

Built with Python, GTK3, and Linux Mint's native XApp.StatusIcon for seamless Cinnamon desktop integration.

## Features

- **Live tray icon** with color-coded arc showing session usage (green/yellow/red); when you're burning faster than the window allows, the over-pace portion of the arc turns red
- **Popup dashboard** (left-click) with detailed stats:
  - Session (5h) and Weekly (7d) usage bars with reset countdowns
  - Per-model usage (Opus, Sonnet)
  - Daily pacing for both session and weekly windows (ahead/under/on pace)
  - Extra usage (overage) status indicator
  - Claude system status (operational/degraded/outage)
  - Available models list
- **Desktop notifications** at usage thresholds (75%, 90%, 100%) — deliberately sparse; pacing is shown in the tray icon instead of via alerts
- **Auto-detects plan changes** (Pro/Max 5x/Max 20x) via live API polling
- **Zero dependencies to install** - uses only packages already on Linux Mint

## Requirements

- Linux Mint with Cinnamon desktop (uses XApp.StatusIcon)
- Python 3
- Claude Code CLI logged in (`claude login`)

All other dependencies (PyGObject, GTK3, XApp, libnotify, requests) are pre-installed on Linux Mint.

## Installation

```bash
git clone https://github.com/lab8xyz/claude-usage-tracker.git
cd claude-usage-tracker
```

**Run it now:**

```bash
./claude-usage-tracker.py &
```

**Auto-start on login:**

```bash
./install.sh
```

This copies a `.desktop` entry to `~/.config/autostart/`.

## How It Works

The app reads your OAuth token from Claude Code's credentials file (`~/.claude/.credentials.json`) and polls the usage API every 60 seconds. No browser session cookies or manual token pasting needed.

### Tray Icon

The circular icon shows your current 5-hour session usage percentage with a color-coded arc:
- **Green** (0-74%) - plenty of capacity
- **Yellow** (75-89%) - getting high
- **Red** (90-100%) - nearly at limit

When usage is ahead of the expected pace for this point in the 5-hour window, the arc splits in two: the part up to the expected level keeps its usage color, and everything beyond it is drawn red. The bigger the red segment, the further ahead of pace you are — red always means "slow down".

Left-click opens the popup dashboard. Right-click for Refresh/Quit.

### Pacing

The popup shows pacing indicators for both session and weekly windows. For example:

```
Session (5h)   80.0%
Hour 4.8/5  -16% under  (expected ~96%)

Weekly         30.0%
Day 2.7/7   -9% under  (expected ~39%)
```

This helps you spread usage evenly instead of burning through limits early.

### Notifications

| Type | Triggers at |
|------|------------|
| Usage thresholds | 75%, 90%, 100% (once each per reset, session and weekly) |

If a single poll jumps across several thresholds at once, only one notification is sent (for the highest). Pacing is never notified — it's shown live in the tray icon arc instead.

## Authentication

The app uses the same OAuth token that Claude Code CLI stores in `~/.claude/.credentials.json`. If the token expires, just run `claude login` to refresh it.

## API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `/api/oauth/usage` | Session, weekly, and model-specific usage percentages |
| `/api/oauth/profile` | Plan tier and subscription info (auto-detects changes) |
| `/api/oauth/account` | Available models list |
| `status.claude.com/api/v2/status.json` | Claude system status |

## Claude Code Statusline

A companion script that displays usage info directly in the Claude Code terminal statusline.

```
 5h: 26% ██░░░░░░░░ | 7d: 32% ███░░░░░░░ | ctx: 45% ███░░░░░
 ● Claude Usage | ⎇ master | Opus £1.23 | → Reset: 12:00
```

**Line 1** — Progress bars for session (5h), weekly (7d), and context window usage, color-coded green/yellow/red.

**Line 2** — Claude system status dot, git branch, model name, session cost, and next reset time.

### Statusline Installation

Add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/claude-statusline.py"
  }
}
```

Requires `requests` (`pip install requests`). Usage data is cached for 60 seconds at `/tmp/claude-statusline-cache.json` to avoid excessive API calls.

## License

MIT
