#!/usr/bin/env python3
"""Tests for the API account spend section (Admin API cost report)."""

import importlib.util
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location("cut", "claude-usage-tracker.py")
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def bucket(day, *amounts_cents):
    return {"starting_at": f"{day}T00:00:00Z", "ending_at": f"{day}T23:59:59Z",
            "results": [{"amount": a, "currency": "USD"} for a in amounts_cents]}


def test_daily_spend_converts_cents_to_dollars_and_sums_results():
    days = cut.daily_spend([bucket("2026-10-16", "123.45", "100"), bucket("2026-10-17")])
    assert set(days) == {"2026-10-16", "2026-10-17"}, days
    assert abs(days["2026-10-16"] - 2.2345) < 1e-9 and days["2026-10-17"] == 0.0, days


def test_summary_today_and_month_ignore_last_month():
    days = {"2026-09-30": 50.0, "2026-10-01": 1.0, "2026-10-17": 2.5}
    s = cut.summarize_api_spend(days, NOW, {})
    assert s["today"] == 2.5 and s["month"] == 3.5, s
    assert s["remaining"] is None, s


def test_remaining_subtracts_spend_since_balance_set_minus_baseline():
    """Balance £20 set on the 16th when $1 had already been spent that day."""
    days = {"2026-10-15": 9.0, "2026-10-16": 4.0, "2026-10-17": 2.5}
    cfg = {"balance_usd": 20.0, "balance_set_day": "2026-10-16", "baseline_usd": 1.0}
    s = cut.summarize_api_spend(days, NOW, cfg)
    assert abs(s["remaining"] - (20.0 - (4.0 + 2.5 - 1.0))) < 1e-9, s


def test_fetch_start_covers_month_and_balance_day():
    assert cut.cost_report_start(NOW, {}) == "2026-10-01T00:00:00Z"
    cfg = {"balance_set_day": "2026-09-20"}
    assert cut.cost_report_start(NOW, cfg) == "2026-09-20T00:00:00Z"


def test_fetch_cost_buckets_follows_pagination():
    pages = {
        None: {"data": [bucket("2026-10-01", "100")], "has_more": True, "next_page": "p2"},
        "p2": {"data": [bucket("2026-10-02", "200")], "has_more": False, "next_page": None},
    }
    seen = []

    def fake_get(params):
        seen.append(params.get("page"))
        return pages[params.get("page")]

    out = cut.fetch_cost_buckets(fake_get, "2026-10-01T00:00:00Z")
    assert [b["starting_at"][:10] for b in out] == ["2026-10-01", "2026-10-02"], out
    assert seen == [None, "p2"], seen


def test_config_roundtrip_is_private():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "api.json"
        assert cut.load_api_config(p) == {}
        cut.save_api_config(p, {"admin_key": "sk-ant-admin01-x", "balance_usd": 5.0})
        assert cut.load_api_config(p)["balance_usd"] == 5.0
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


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
