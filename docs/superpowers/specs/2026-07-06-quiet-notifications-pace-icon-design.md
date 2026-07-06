# Quiet Notifications + Pace-Aware Tray Icon

**Date:** 2026-07-06
**Problem:** The tracker sent ~20 desktop notifications in a day. Two causes:
pacing alerts fire at +10% ahead and every +5% after (with a burst bug that
sends every missed threshold in a single poll), and usage-threshold
notifications fire every 5% from 75 for both session and weekly windows.

## Design

1. **Two-tone pace arc (tray icon).** `render_icon()` gains an
   `expected_pct` parameter. When session usage exceeds the expected level
   for this point in the 5-hour window, the arc is drawn in two segments:
   0→expected in the normal usage colour, expected→actual in red. Under or
   on pace, the icon is unchanged from today. If expected pace cannot be
   computed (no reset timestamp from the API), fall back to the current
   single-colour arc. Chosen from mockups (variant B) over a pace-coloured
   centre number and a combined variant.

2. **Pacing notifications removed.** `_check_pace_notifications()`, its
   tracking sets, and the now-unused `PACE_STEP`/`PACE_GRACE_MINUTES`
   constants are deleted. The icon carries the pacing signal; the popup's
   pacing rows are unchanged (`PACE_FIRST_THRESHOLD` stays for popup
   styling).

3. **Threshold notifications trimmed.** `NOTIFY_THRESHOLDS` becomes
   `[75, 90, 100]` (was every 5% from 75), still once each per reset cycle,
   for both session and weekly windows.

4. **Burst fix.** When one poll crosses several thresholds at once, mark
   them all as notified but send a single notification for the highest —
   instead of one notification per threshold.

5. **README** updated to match; running tracker restarted on the new code.

**Worst case after change:** 6 notifications per 5h cycle (3 session +
3 weekly), realistically 2–3 per day.
