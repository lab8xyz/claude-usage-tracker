#!/usr/bin/env python3
"""Tests for model-scoped weekly limits (e.g. the Fable weekly allowance).

The usage API reports these only inside `limits` as kind "weekly_scoped",
not as a top-level seven_day_<model> bucket.
"""

import importlib.util

_spec = importlib.util.spec_from_file_location("cut", "claude-usage-tracker.py")
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)

FABLE_RESET = "2026-10-19T15:59:59.596768+00:00"

SAMPLE = {
    "five_hour": {"utilization": 17.0, "resets_at": "2026-10-17T12:49:59+00:00"},
    "seven_day": {"utilization": 73.0, "resets_at": "2026-10-19T15:59:59+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 17,
         "resets_at": "2026-10-17T12:49:59+00:00", "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 73,
         "resets_at": "2026-10-19T15:59:59+00:00", "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 94,
         "severity": "critical", "resets_at": FABLE_RESET,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": True},
    ],
}


def test_scoped_weekly_parses_fable_limit():
    data = cut.UsageData(SAMPLE)
    assert data.scoped_weekly == [("Fable", 94.0, FABLE_RESET)], data.scoped_weekly


def test_scoped_weekly_skips_unscoped_limits():
    """Only weekly_scoped entries count; session and weekly_all are ignored."""
    data = cut.UsageData(SAMPLE)
    assert len(data.scoped_weekly) == 1, data.scoped_weekly


def test_scoped_weekly_empty_when_limits_missing_or_null():
    assert cut.UsageData({"five_hour": {"utilization": 1}}).scoped_weekly == []
    assert cut.UsageData({"limits": None}).scoped_weekly == []


def test_scoped_weekly_label_falls_back_to_surface():
    raw = {"limits": [{"kind": "weekly_scoped", "percent": 10, "resets_at": FABLE_RESET,
                       "scope": {"model": None, "surface": "cowork"}}]}
    assert cut.UsageData(raw).scoped_weekly == [("cowork", 10.0, FABLE_RESET)]


def test_scoped_notifications_fire_once_per_threshold():
    """Fable at 94% announces 90 once; the same poll again says nothing."""
    state = {}
    scoped = [("Fable", 94.0, FABLE_RESET)]
    first = cut.evaluate_scoped_notifications(scoped, state)
    assert first == [("Fable", 94.0, FABLE_RESET, 90)], first
    again = cut.evaluate_scoped_notifications(scoped, state)
    assert again == [], again


def test_scoped_notifications_escalate_to_100():
    state = {}
    cut.evaluate_scoped_notifications([("Fable", 94.0, FABLE_RESET)], state)
    hit = cut.evaluate_scoped_notifications([("Fable", 100.0, FABLE_RESET)], state)
    assert hit == [("Fable", 100.0, FABLE_RESET, 100)], hit


def test_scoped_notifications_track_labels_independently():
    state = {}
    out = cut.evaluate_scoped_notifications(
        [("Fable", 94.0, FABLE_RESET), ("Opus", 10.0, FABLE_RESET)], state)
    assert [o[0] for o in out] == ["Fable"], out


def test_fable_property_and_missing_session():
    data = cut.UsageData(SAMPLE)
    assert data.fable == (94.0, FABLE_RESET)
    assert data.has_session
    no_session = cut.UsageData(dict(SAMPLE, five_hour=None, limits=[]))
    assert not no_session.has_session
    assert no_session.fable is None


def test_pace_phrase():
    assert cut.pace_phrase(None) == ""
    assert cut.pace_phrase((1, 7, "d", 30.0, 15.4)) == " (15% ahead of pace)"
    assert cut.pace_phrase((1, 7, "d", 30.0, -20.0)) == " (20% under pace)"
    assert cut.pace_phrase((1, 7, "d", 30.0, 0.5)) == " (on pace)"


def test_render_icon_two_rings():
    pb = cut.render_icon(45, 30, 60, 30)
    assert pb.get_width() == cut.ICON_SIZE


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
