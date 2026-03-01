#!/bin/bash
# Install Claude Usage Tracker autostart entry

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DESKTOP_FILE="$SCRIPT_DIR/claude-usage-tracker.desktop"
AUTOSTART_DIR="$HOME/.config/autostart"

# Update Exec path in desktop file to use absolute path
sed -i "s|^Exec=.*|Exec=$SCRIPT_DIR/claude-usage-tracker.py|" "$DESKTOP_FILE"

# Install to autostart
mkdir -p "$AUTOSTART_DIR"
cp "$DESKTOP_FILE" "$AUTOSTART_DIR/"

echo "Installed to $AUTOSTART_DIR/claude-usage-tracker.desktop"
echo "Claude Usage Tracker will start automatically on next login."
echo ""
echo "To start it now, run:"
echo "  $SCRIPT_DIR/claude-usage-tracker.py &"
