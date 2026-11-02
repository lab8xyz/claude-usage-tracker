#!/usr/bin/env python3
"""Tests for the watcher that flags usage API fields gaining data."""

import importlib.util

_spec = importlib.util.spec_from_file_location("cut", "claude-usage-tracker.py")
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)

RAW = {"five_hour": None, "seven_day": {"utilization": 1.0}, "amber_ladder": None,
       "limits": [{"kind": "weekly_all"}], "promo_credits": None, "_ts": 1}


def test_first_run_sets_baseline_silently():
    state, newly = cut.detect_new_fields(RAW, {})
    assert newly == []
    assert state == {"baseline": ["limits", "seven_day"], "discovered": {}}, state


def test_field_gaining_data_is_reported_once():
    state, _ = cut.detect_new_fields(RAW, {})
    raw2 = dict(RAW, amber_ladder={"expires_at": "2026-11-06T14:00:00Z"})
    state, newly = cut.detect_new_fields(raw2, state)
    assert newly == ["amber_ladder"]
    assert "amber_ladder" in state["discovered"]
    state, newly = cut.detect_new_fields(raw2, state)
    assert newly == []


def test_brand_new_key_is_reported():
    state, _ = cut.detect_new_fields(RAW, {})
    _, newly = cut.detect_new_fields(dict(RAW, limit_reset={"x": 1}), state)
    assert newly == ["limit_reset"]


def test_empty_values_and_private_keys_ignored():
    state, _ = cut.detect_new_fields(RAW, {})
    _, newly = cut.detect_new_fields(dict(RAW, promo_credits=[], _status="none"), state)
    assert newly == []


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
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
