#!/usr/bin/env python3
"""Regression tests for usage-threshold notification dedup.

Focus: at 100% a flapping/re-anchoring API response must not re-fire the
75/90/100 threshold notifications. See evaluate_window_notifications.
"""

import importlib.util

_spec = importlib.util.spec_from_file_location("cut", "claude-usage-tracker.py")
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)

THRESHOLDS = cut.NOTIFY_THRESHOLDS


def old_logic(pct, norm_reset, last_reset, notified):
    """Reproduction of the previous (buggy) inline dedup, for evidence."""
    if norm_reset != last_reset:
        notified.clear()
        last_reset = norm_reset
    crossed = [t for t in THRESHOLDS if pct >= t and t not in notified]
    notified.update(crossed)
    return last_reset, crossed


def run_sequence(fn, polls):
    """Drive a poll sequence through `fn`, return count of notification events."""
    last = None
    notified = set()
    events = 0
    for pct, norm_reset in polls:
        last, crossed = fn(pct, norm_reset, last, notified)
        if crossed:
            events += 1
    return events


# A poll sequence at 100% where the window bucket flaps present -> null -> present.
FLAP_AT_100 = [
    (100.0, "2026-07-11T21:30:00+00:00"),  # first cross: notify once
    (0.0, None),                            # bucket transiently null/missing
    (100.0, "2026-07-11T21:30:00+00:00"),  # bucket back, same window
    (0.0, None),
    (100.0, "2026-07-11T21:30:00+00:00"),
    (0.0, None),
    (100.0, "2026-07-11T21:30:00+00:00"),
]


def test_old_logic_floods():
    """Documents the bug: the old logic re-fires on every flap."""
    events = run_sequence(old_logic, FLAP_AT_100)
    assert events > 1, f"expected the old logic to flood, got {events} events"
    print(f"  [evidence] old logic fires {events} notifications on flap sequence")


def test_flap_fires_once():
    """A flapping window at 100% must notify exactly once."""
    events = run_sequence(cut.evaluate_window_notifications, FLAP_AT_100)
    assert events == 1, f"expected exactly 1 notification, got {events}"


def test_backward_wobble_does_not_refire():
    """A reset time wobbling earlier then back must not re-arm thresholds."""
    polls = [
        (100.0, "2026-07-11T21:30:00+00:00"),
        (100.0, "2026-07-11T21:29:00+00:00"),  # earlier wobble
        (100.0, "2026-07-11T21:30:00+00:00"),  # back to real value
    ]
    events = run_sequence(cut.evaluate_window_notifications, polls)
    assert events == 1, f"expected 1 notification across wobble, got {events}"


def test_new_window_refires():
    """A genuinely new, later window re-arms and notifies again."""
    polls = [
        (100.0, "2026-07-11T21:30:00+00:00"),   # window 1 hits 100
        (30.0, "2026-07-12T02:30:00+00:00"),    # window reset 5h later, low usage
        (100.0, "2026-07-12T02:30:00+00:00"),   # climbs to 100 again
    ]
    events = run_sequence(cut.evaluate_window_notifications, polls)
    assert events == 2, f"expected 2 notifications across 2 windows, got {events}"


def test_gradual_climb_notifies_each_threshold_once():
    """Escalating usage in one window notifies at each threshold, once each."""
    polls = [
        (76.0, "2026-07-11T21:30:00+00:00"),  # crosses 75
        (91.0, "2026-07-11T21:30:00+00:00"),  # crosses 90
        (100.0, "2026-07-11T21:30:00+00:00"),  # crosses 100
        (100.0, "2026-07-11T21:30:00+00:00"),  # steady, no repeat
    ]
    events = run_sequence(cut.evaluate_window_notifications, polls)
    assert events == 3, f"expected 3 escalating notifications, got {events}"


def test_highest_threshold_reported_on_burst():
    """Crossing several thresholds in one poll reports the highest."""
    last, crossed = cut.evaluate_window_notifications(
        100.0, "2026-07-11T21:30:00+00:00", None, set())
    assert crossed and max(crossed) == 100, crossed


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
