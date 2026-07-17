import asyncio
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.usage_estimate as usage_estimate
from app.core.database import Base
from app.models import UsageLimitSample
from app.services.usage_estimate import (
    _account_estimate,
    account_rate_limited_windows,
    _aggregate_window,
    _limit_calibration,
    _load_usage_limit_calibrations,
    _normalize_plan_cohort,
    _plan_cohort_from_account,
    _refreshed_window_state_values,
    _resolve_limit_calibration,
    _save_usage_limit_samples,
    _usage_limit_sample_updates,
    _usage_limit_sample_allowed,
    _usage_token_window_observations,
    _window_effective_spent,
    _window_estimate,
    _estimate_window_values,
    _window_reset_detected,
)
from app.services.sub2api import Sub2ApiClient


class UsageEstimateTests(unittest.TestCase):
    def test_account_estimate_exposes_sub2api_name_separately_from_email(self) -> None:
        account = {
            "id": "1",
            "name": "Campus Plus Account",
            "credentials": {"email": "student@example.edu", "plan_type": "plus"},
            "extra": {"codex_5h_window_minutes": 0, "codex_7d_window_minutes": 10_080},
        }
        calibrations = {
            "seven_day": {
                "plus": _limit_calibration("seven_day", [], "plus"),
                "unknown": _limit_calibration("seven_day", [], "unknown"),
            }
        }

        row = _account_estimate(
            account=account,
            group_map={},
            sub2api=Sub2ApiClient(),
            usage_estimate_enabled=True,
            usage={},
            usage_error=None,
            usage_states={},
            limit_calibrations=calibrations,
            token_history={},
        )

        self.assertEqual(row["account_name"], "Campus Plus Account")
        self.assertEqual(row["email"], "student@example.edu")

    def test_small_reset_at_drift_does_not_count_as_reset(self) -> None:
        self.assertFalse(
            _window_reset_detected(
                window_key="seven_day",
                current_reset_at="2026-06-08T07:13:00Z",
                last_reset_at="2026-06-08T07:16:00Z",
                current_remaining_seconds=560764,
                last_remaining_seconds=560952,
                current_used_percent=17.0,
                last_used_percent=17.0,
            )
        )

    def test_large_reset_at_jump_counts_as_reset(self) -> None:
        self.assertTrue(
            _window_reset_detected(
                window_key="seven_day",
                current_reset_at="2026-06-15T07:13:00Z",
                last_reset_at="2026-06-08T07:13:00Z",
                current_remaining_seconds=560764,
                last_remaining_seconds=120,
                current_used_percent=17.0,
                last_used_percent=98.0,
            )
        )

    def test_stale_delta_correction_self_heals(self) -> None:
        usage_states = {
            ("id:1", "seven_day"): {
                "account_key": "id:1",
                "window_key": "seven_day",
                "baseline_spent": 117.89462665,
                "estimate_uses_spent_delta": True,
                "last_raw_spent": 117.89462665,
                "last_used_percent": 68.0,
                "last_reset_at": "2026-06-06T03:12:17Z",
                "last_remaining_seconds": 373709,
            }
        }

        spent, baseline_spent, uses_delta = _window_effective_spent(
            account_key="id:1",
            account_id="1",
            email="anthonykim1512@outlook.com",
            window_key="seven_day",
            raw_spent=117.89462665,
            used_percent=68.0,
            reset_at="2026-06-06T03:12:17Z",
            remaining_seconds=373521,
            usage_states=usage_states,
        )

        self.assertAlmostEqual(spent or 0.0, 117.89462665)
        self.assertEqual(baseline_spent, 0.0)
        self.assertFalse(uses_delta)
        state = usage_states[("id:1", "seven_day")]
        self.assertEqual(state["baseline_spent"], 0.0)
        self.assertFalse(state["estimate_uses_spent_delta"])

    def test_legitimate_delta_correction_is_kept_for_large_cumulative_spend(self) -> None:
        usage_states = {
            ("id:2", "five_hour"): {
                "account_key": "id:2",
                "window_key": "five_hour",
                "baseline_spent": 100.0,
                "estimate_uses_spent_delta": True,
                "last_raw_spent": 100.0,
                "last_used_percent": 100.0,
                "last_reset_at": "2026-06-01T10:00:00Z",
                "last_remaining_seconds": 60,
            }
        }

        spent, baseline_spent, uses_delta = _window_effective_spent(
            account_key="id:2",
            account_id="2",
            email="example@example.com",
            window_key="five_hour",
            raw_spent=150.0,
            used_percent=20.0,
            reset_at="2026-06-01T10:00:00Z",
            remaining_seconds=18000,
            usage_states=usage_states,
        )

        self.assertAlmostEqual(spent or 0.0, 50.0)
        self.assertEqual(baseline_spent, 100.0)
        self.assertTrue(uses_delta)

    def test_usage_derived_limit_is_clipped_to_calibration(self) -> None:
        window = _window_estimate(
            account={"email": "example@example.com", "status": "active", "schedulable": True},
            usage={
                "seven_day": {
                    "used_percent": 70.0,
                    "remaining_seconds": 123,
                    "reset_at": "2026-06-06T03:12:17Z",
                    "window_stats": {"cost": 112.0},
                }
            },
            window_key="seven_day",
            account_key="id:3",
            account_id="3",
            email="example@example.com",
            plan_cohort="team",
            usage_states={},
            limit_calibrations={
                "seven_day": {
                    "team": {"lower": 100.0, "upper": 140.0, "mean": None, "sigma": None},
                    "unknown": {"lower": 100.0, "upper": 140.0, "mean": None, "sigma": None},
                },
            },
        )

        self.assertAlmostEqual(window["estimated_limit"], 140.0)
        self.assertAlmostEqual(window["spent"], 112.0)
        self.assertAlmostEqual(window["estimate_spent"], 112.0)
        self.assertAlmostEqual(window["remaining"], 42.0)
        self.assertAlmostEqual(window["remaining_percent"], 30.0)
        self.assertEqual(window["estimate_basis"], "official_window_clipped")

    def test_usage_derived_limit_is_not_clipped_below_spent(self) -> None:
        window = _window_estimate(
            account={"email": "zacharybell2573@hotmail.com", "status": "active", "schedulable": True},
            usage={
                "seven_day": {
                    "used_percent": 68.0,
                    "remaining_seconds": 123,
                    "reset_at": "2026-06-06T03:12:17Z",
                    "window_stats": {"cost": 154.30},
                }
            },
            window_key="seven_day",
            account_key="id:zachary",
            account_id="zachary",
            email="zacharybell2573@hotmail.com",
            plan_cohort="team",
            usage_states={},
            limit_calibrations={
                "seven_day": {
                    "team": {"lower": 100.0, "upper": 140.0, "mean": None, "sigma": None},
                    "unknown": {"lower": 100.0, "upper": 140.0, "mean": None, "sigma": None},
                },
            },
        )

        expected_limit = 154.30 / 0.68
        self.assertAlmostEqual(window["estimated_limit"], expected_limit)
        self.assertAlmostEqual(window["spent"], 154.30)
        self.assertAlmostEqual(window["estimate_spent"], 154.30)
        self.assertAlmostEqual(window["remaining"], expected_limit * 0.32)
        self.assertAlmostEqual(window["remaining_percent"], 32.0)
        self.assertEqual(window["estimate_basis"], "official_window")

    def test_five_hour_limit_clips_usage_derived_total_to_calibration(self) -> None:
        window = _window_estimate(
            account={"email": "example@example.com", "status": "active", "schedulable": True},
            usage={
                "five_hour": {
                    "used_percent": 84.0,
                    "remaining_seconds": 6094,
                    "reset_at": "2026-06-05T11:19:21Z",
                    "window_stats": {"cost": 22.0},
                }
            },
            window_key="five_hour",
            account_key="id:696",
            account_id="696",
            email="example@example.com",
            plan_cohort="plus",
            usage_states={},
            limit_calibrations={
                "five_hour": {
                    "plus": {"lower": 15.0, "upper": 25.0, "mean": None, "sigma": None},
                    "unknown": {"lower": 15.0, "upper": 25.0, "mean": None, "sigma": None},
                },
            },
        )

        self.assertAlmostEqual(window["estimated_limit"], 25.0)
        self.assertAlmostEqual(window["spent"], 22.0)
        self.assertAlmostEqual(window["estimate_spent"], 22.0)
        self.assertAlmostEqual(window["remaining"], 4.0)
        self.assertAlmostEqual(window["remaining_percent"], 16.0)
        self.assertEqual(window["estimate_basis"], "official_window_clipped")

    def test_usage_derived_limit_ignores_conflicting_smaller_raw_limit(self) -> None:
        window = _window_estimate(
            account={"email": "zacharybell2573@hotmail.com", "status": "active", "schedulable": True},
            usage={
                "five_hour": {
                    "used_percent": 80.0,
                    "remaining_seconds": 6094,
                    "reset_at": "2026-06-05T11:19:21Z",
                    "window_stats": {"cost": 37.52, "limit": 25.0},
                }
            },
            window_key="five_hour",
            account_key="id:zachary",
            account_id="zachary",
            email="zacharybell2573@hotmail.com",
            plan_cohort="plus",
            usage_states={},
            limit_calibrations={
                "five_hour": {
                    "plus": {"lower": 10.31, "upper": 30.34, "mean": 20.325, "sigma": 3.3383333333333334},
                    "unknown": {"lower": 15.0, "upper": 25.0, "mean": None, "sigma": None},
                },
            },
        )

        self.assertAlmostEqual(window["estimated_limit"], 46.9)
        self.assertAlmostEqual(window["spent"], 37.52)
        self.assertAlmostEqual(window["estimate_spent"], 37.52)
        self.assertAlmostEqual(window["remaining"], 9.38)
        self.assertAlmostEqual(window["remaining_percent"], 20.0)
        self.assertEqual(window["estimate_basis"], "official_window")

    def test_zero_usage_ignores_zero_raw_limit_and_uses_sample_mean(self) -> None:
        account = {"email": "unused@example.com", "status": "active", "schedulable": True}
        expected_limits = {"five_hour": 21.0, "seven_day": 140.0}
        calibrations = {
            "five_hour": {
                "plus": {"lower": 18.0, "upper": 24.0, "mean": 21.0, "sigma": 1.0},
            },
            "seven_day": {
                "plus": {"lower": 120.0, "upper": 160.0, "mean": 140.0, "sigma": 5.0},
            },
        }
        windows = {}

        for window_key, expected_limit in expected_limits.items():
            windows[window_key] = _window_estimate(
                account=account,
                usage={
                    window_key: {
                        "used_percent": 0.0,
                        "remaining_seconds": 1_000,
                        "window_stats": {"cost": 0.0, "limit": 0.0},
                    }
                },
                window_key=window_key,
                account_key="id:unused",
                account_id="unused",
                email="unused@example.com",
                plan_cohort="plus",
                usage_states={},
                limit_calibrations=calibrations,
            )

            self.assertAlmostEqual(windows[window_key]["spent"], 0.0)
            self.assertAlmostEqual(windows[window_key]["estimated_limit"], expected_limit)
            self.assertAlmostEqual(windows[window_key]["remaining"], expected_limit)
            self.assertEqual(windows[window_key]["estimate_basis"], "sample_limit_zero_usage")

        row = {
            "usage_estimate_enabled": True,
            "deactive": False,
            "error": None,
            "usage_error": None,
            "rate_limited": False,
            **windows,
        }
        for window_key, expected_limit in expected_limits.items():
            aggregate = _aggregate_window([row], window_key)
            self.assertAlmostEqual(aggregate["estimated_limit"], expected_limit)
            self.assertAlmostEqual(aggregate["remaining"], expected_limit)

    def test_remaining_seconds_without_reset_at_derives_reset_at(self) -> None:
        before = usage_estimate.utcnow()
        usage = usage_estimate.materialize_usage_reset_times(
            {
                "five_hour": {
                    "used_percent": 50.0,
                    "remaining_seconds": 90,
                    "window_stats": {"cost": 10.0},
                }
            }
        )
        window = _window_estimate(
            account={"email": "example@example.com", "status": "active", "schedulable": True},
            usage=usage,
            window_key="five_hour",
            account_key="id:90",
            account_id="90",
            email="example@example.com",
            plan_cohort="plus",
            usage_states={},
            limit_calibrations={
                "five_hour": {
                    "plus": _limit_calibration("five_hour", [], "plus"),
                    "unknown": _limit_calibration("five_hour", [], "unknown"),
                },
            },
        )
        after = usage_estimate.utcnow()

        reset_at = usage_estimate._parse_datetime(window["reset_at"])
        self.assertIsNotNone(reset_at)
        self.assertGreaterEqual(reset_at.timestamp(), before.timestamp() + 89)
        self.assertLessEqual(reset_at.timestamp(), after.timestamp() + 90)

    def test_full_window_limit_uses_official_raw_total_without_clipping(self) -> None:
        window = _window_estimate(
            account={"email": "example@example.com", "status": "active", "schedulable": True},
            usage={
                "five_hour": {
                    "used_percent": 100.0,
                    "remaining_seconds": 4034,
                    "reset_at": "2099-06-05T11:47:11Z",
                    "window_stats": {"cost": 33.44289765},
                }
            },
            window_key="five_hour",
            account_key="id:714",
            account_id="714",
            email="example@example.com",
            plan_cohort="plus",
            usage_states={},
            limit_calibrations={
                "five_hour": {
                    "plus": {"lower": 15.0, "upper": 25.0, "mean": None, "sigma": None},
                    "unknown": {"lower": 15.0, "upper": 25.0, "mean": None, "sigma": None},
                },
            },
        )

        self.assertAlmostEqual(window["estimated_limit"], 33.44289765)
        self.assertAlmostEqual(window["raw_spent"], 33.44289765)
        self.assertAlmostEqual(window["estimate_spent"], 33.44289765)
        self.assertAlmostEqual(window["remaining"], 0.0)
        self.assertTrue(window["rate_limited"])
        self.assertEqual(window["estimate_basis"], "official_window")

    def test_account_snapshot_clears_zero_spend_stale_limit(self) -> None:
        account = {
            "email": "example@example.com",
            "status": "active",
            "schedulable": True,
            "extra": {
                "codex_5h_used_percent": 1.0,
                "codex_5h_reset_at": "2099-06-05T12:54:25Z",
                "codex_5h_reset_after_seconds": 18000,
            },
        }
        usage = {
            "five_hour": {
                "used_percent": 100.0,
                "reset_at": "2099-06-05T12:59:36Z",
                "remaining_seconds": 17680,
                "window_stats": {"cost": 0.0},
            }
        }

        window = _window_estimate(
            account=account,
            usage=usage,
            window_key="five_hour",
            account_key="id:713",
            account_id="713",
            email="example@example.com",
            plan_cohort="plus",
            usage_states={},
            limit_calibrations={
                "five_hour": {
                    "plus": _limit_calibration("five_hour", [], "plus"),
                    "unknown": _limit_calibration("five_hour", [], "unknown"),
                },
            },
        )

        self.assertEqual(account_rate_limited_windows(account, usage), [])
        self.assertFalse(window["rate_limited"])
        self.assertEqual(window["source"], "account")
        self.assertAlmostEqual(window["used_percent"] or 0.0, 1.0)
        self.assertAlmostEqual(window["raw_spent"] or 0.0, 0.0)

        refreshed = _refreshed_window_state_values(account, usage, "five_hour")
        self.assertAlmostEqual(refreshed["used_percent"] or 0.0, 1.0)
        self.assertAlmostEqual(refreshed["raw_spent"] or 0.0, 0.0)

    def test_positive_spend_full_window_still_counts_as_limited(self) -> None:
        account = {
            "email": "example@example.com",
            "status": "active",
            "schedulable": True,
            "extra": {"codex_5h_used_percent": 1.0},
        }
        usage = {
            "five_hour": {
                "used_percent": 100.0,
                "reset_at": "2099-06-05T12:59:36Z",
                "window_stats": {"cost": 20.0},
            }
        }

        self.assertEqual(account_rate_limited_windows(account, usage), ["five_hour"])

    def test_custom_threshold_counts_window_as_rate_limited_before_full_percent(self) -> None:
        account = {
            "email": "example@example.com",
            "status": "active",
            "schedulable": True,
        }
        usage = {
            "five_hour": {
                "used_percent": 95.0,
                "reset_at": "2099-06-05T12:59:36Z",
                "window_stats": {"cost": 19.0},
            }
        }

        self.assertEqual(account_rate_limited_windows(account, usage), [])
        self.assertEqual(account_rate_limited_windows(account, usage, {"five_hour": 95}), ["five_hour"])

        default_window = _window_estimate(
            account=account,
            usage=usage,
            window_key="five_hour",
            account_key="id:95",
            account_id="95",
            email="example@example.com",
            plan_cohort="plus",
            usage_states={},
            limit_calibrations={
                "five_hour": {
                    "plus": _limit_calibration("five_hour", [], "plus"),
                    "unknown": _limit_calibration("five_hour", [], "unknown"),
                },
            },
        )
        threshold_window = _window_estimate(
            account=account,
            usage=usage,
            window_key="five_hour",
            account_key="id:95",
            account_id="95",
            email="example@example.com",
            plan_cohort="plus",
            usage_states={},
            limit_calibrations={
                "five_hour": {
                    "plus": _limit_calibration("five_hour", [], "plus"),
                    "unknown": _limit_calibration("five_hour", [], "unknown"),
                },
            },
            sample_thresholds={"five_hour": 95},
        )

        self.assertFalse(default_window["rate_limited"])
        self.assertTrue(threshold_window["rate_limited"])

    def test_integer_utilization_is_treated_as_percent_not_fraction(self) -> None:
        account = {
            "email": "example@example.com",
            "status": "active",
            "schedulable": True,
        }
        usage = {
            "seven_day": {
                "utilization": 1,
                "reset_at": "2099-07-01T00:00:00Z",
                "remaining_seconds": 1_000_000,
                "window_stats": {"cost": 0.0},
            }
        }

        window = _window_estimate(
            account=account,
            usage=usage,
            window_key="seven_day",
            account_key="id:1",
            account_id="1",
            email="example@example.com",
            plan_cohort="team",
            usage_states={},
            limit_calibrations={
                "seven_day": {
                    "team": _limit_calibration("seven_day", [], "team"),
                    "unknown": _limit_calibration("seven_day", [], "unknown"),
                },
            },
        )

        self.assertAlmostEqual(window["used_percent"] or 0.0, 1.0)
        self.assertFalse(window["rate_limited"])
        self.assertEqual(account_rate_limited_windows(account, usage), [])

    def test_monthly_limit_reports_monthly_window_not_five_hour(self) -> None:
        account = {
            "id": "1",
            "email": "team@example.com",
            "status": "active",
            "schedulable": True,
            "credentials": {"plan_type": "team"},
            "extra": {"codex_5h_window_minutes": 0},
        }
        usage = {
            "monthly": {
                "used_percent": 100.0,
                "remaining_seconds": 1_000_000,
                "reset_at": "2099-07-01T00:00:00Z",
                "window_stats": {"cost": 200.0},
            }
        }

        self.assertEqual(account_rate_limited_windows(account, usage), ["monthly"])

    def test_monthly_usage_token_history_is_recorded_as_monthly_window(self) -> None:
        class FakeSub2Api:
            def account_id(self, account):
                return account.get("id")

            def account_email(self, account):
                return account.get("email")

        account = {
            "id": "1",
            "email": "team@example.com",
            "status": "active",
            "schedulable": True,
            "credentials": {"plan_type": "team"},
            "extra": {"codex_5h_window_minutes": 0},
        }
        usage = {
            "monthly": {
                "used_percent": 50.0,
                "remaining_seconds": 1_000_000,
                "reset_at": "2099-07-01T00:00:00Z",
                "window_stats": {"cost": 120.0, "tokens": 120_000},
            }
        }

        observations = _usage_token_window_observations([account], FakeSub2Api(), {"1": usage})

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["window_key"], "monthly")
        self.assertEqual(observations[0]["window_reset_key"], "reset_date:2099-07-01")
        self.assertEqual(observations[0]["window_start_at"], "2099-06-01T00:00:00Z")
        self.assertEqual(observations[0]["tokens"], 120_000)
        self.assertAlmostEqual(observations[0]["spent"], 120.0)

    def test_plan_cohort_normalization(self) -> None:
        self.assertEqual(_normalize_plan_cohort("chatgptplusplan"), "plus")
        self.assertEqual(_normalize_plan_cohort("team"), "team")
        self.assertEqual(_normalize_plan_cohort("ChatGPTTeamPlan"), "team")
        self.assertEqual(_normalize_plan_cohort("ChatGPTK12Plan"), "k12")
        self.assertEqual(_normalize_plan_cohort("ChatGPT Enterprise Edu Plan"), "enterprise-edu")
        self.assertEqual(_normalize_plan_cohort(""), "unknown")
        self.assertEqual(_plan_cohort_from_account({"email": "ctf1du13uabx1@gptteam.ikun.edu.rs"}), "team")

    def test_resolve_limit_calibration_prefers_matching_cohort(self) -> None:
        calibrations = {
            "seven_day": {
                "plus": _limit_calibration("seven_day", [150.0] * 100, "plus"),
                "team": _limit_calibration("seven_day", [110.0] * 100, "team"),
                "unknown": _limit_calibration("seven_day", [], "unknown"),
            }
        }

        team_calibration = _resolve_limit_calibration("seven_day", calibrations, "team")
        plus_calibration = _resolve_limit_calibration("seven_day", calibrations, "plus")
        fallback_calibration = _resolve_limit_calibration("seven_day", calibrations, "free")
        unknown_calibration = _resolve_limit_calibration("seven_day", calibrations, "unknown")

        self.assertAlmostEqual(team_calibration["lower"], 110.0)
        self.assertAlmostEqual(plus_calibration["lower"], 150.0)
        self.assertEqual(fallback_calibration["plan_cohort"], "free")
        self.assertEqual(unknown_calibration["plan_cohort"], "unknown")

    def test_usage_limit_sample_allowed_rejects_unknown_and_below_default_lower_bound(self) -> None:
        self.assertFalse(_usage_limit_sample_allowed("five_hour", "unknown", 20.0))
        self.assertFalse(_usage_limit_sample_allowed("five_hour", "plus", 9.99))
        self.assertFalse(_usage_limit_sample_allowed("seven_day", "team", 99.99))
        self.assertFalse(_usage_limit_sample_allowed("monthly", "plus", 399.99))
        self.assertTrue(_usage_limit_sample_allowed("monthly", "plus", 400.0))
        self.assertTrue(_usage_limit_sample_allowed("five_hour", "plus", 15.0))
        self.assertTrue(_usage_limit_sample_allowed("seven_day", "team", 100.0))
        self.assertTrue(_usage_limit_sample_allowed("monthly", "team", 100.0))
        self.assertTrue(_usage_limit_sample_allowed("five_hour", "k12", 15.0))
        self.assertTrue(_usage_limit_sample_allowed("monthly", "enterprise-edu", 400.0))

    def test_custom_k12_default_range_is_used_without_samples(self) -> None:
        ranges = {
            "k12": {
                "five_hour": {"lower": 30.0, "upper": 45.0},
                "seven_day": {"lower": 200.0, "upper": 260.0},
                "monthly": {"lower": 500.0, "upper": 700.0},
            }
        }

        calibration = _limit_calibration("seven_day", [], "ChatGPTK12Plan", ranges)

        self.assertEqual(calibration["plan_cohort"], "k12")
        self.assertEqual(calibration["source"], "default")
        self.assertAlmostEqual(calibration["lower"], 200.0)
        self.assertAlmostEqual(calibration["upper"], 260.0)

    def test_k12_usage_limit_sample_keeps_subscription_type(self) -> None:
        class FakeSub2Api:
            def account_id(self, account):
                return account.get("id")

            def account_email(self, account):
                return account.get("email")

        account = {
            "id": "k12-1",
            "email": "student@example.edu",
            "credentials": {"plan_type": "ChatGPTK12Plan"},
        }
        usage = {
            "five_hour": {
                "used_percent": 100.0,
                "reset_at": "2099-01-01T05:00:00Z",
                "window_stats": {"cost": 35.0},
            }
        }

        samples = _usage_limit_sample_updates([account], FakeSub2Api(), {"k12-1": usage})

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["plan_cohort"], "k12")
        self.assertAlmostEqual(samples[0]["observed_limit"], 35.0)

    def test_custom_five_hour_sample_threshold_collects_before_full_percent(self) -> None:
        class FakeSub2Api:
            def account_id(self, account):
                return account.get("id")

            def account_email(self, account):
                return account.get("email")

        account = {
            "id": "1",
            "email": "plus@example.com",
            "credentials": {"plan_type": "plus"},
        }
        usage = {
            "five_hour": {
                "used_percent": 95.0,
                "reset_at": "2099-01-01T05:00:00Z",
                "window_stats": {"cost": 19.0},
            }
        }

        default_samples = _usage_limit_sample_updates([account], FakeSub2Api(), {"1": usage}, {"five_hour": 0})
        custom_samples = _usage_limit_sample_updates([account], FakeSub2Api(), {"1": usage}, {"five_hour": 95})

        self.assertEqual(default_samples, [])
        self.assertEqual(len(custom_samples), 1)
        self.assertEqual(custom_samples[0]["window_key"], "five_hour")
        self.assertAlmostEqual(custom_samples[0]["observed_limit"], 20.0)
        self.assertAlmostEqual(custom_samples[0]["used_percent"], 95.0)

    def test_plus_without_five_hour_keeps_exact_seven_day_window(self) -> None:
        account = {
            "id": "2320",
            "email": "plus@example.com",
            "credentials": {"plan_type": "plus"},
            "extra": {"codex_5h_window_minutes": 0, "codex_7d_window_minutes": 10_080},
        }
        usage = {
            "five_hour": {"window_minutes": 0},
            "seven_day": {
                "used_percent": 75.0,
                "window_minutes": 10_080,
                "resets_at": "2026-07-20T03:05:32+08:00",
                "remaining_seconds": 603_602,
                "window_stats": {"cost": 90.0},
            },
        }

        _, _, five_hour_kind = _estimate_window_values(account, usage, "five_hour")
        seven_day_values, _, seven_day_kind = _estimate_window_values(account, usage, "seven_day")

        self.assertEqual(five_hour_kind, "none")
        self.assertEqual(seven_day_kind, "seven_day")
        self.assertEqual(seven_day_values["window_minutes"], 10_080)
        self.assertEqual(seven_day_values["reset_at"], "2026-07-20T03:05:32+08:00")
        self.assertEqual(seven_day_values["remaining_seconds"], 603_602)

    def test_plus_without_five_hour_collects_weekly_not_monthly_sample(self) -> None:
        class FakeSub2Api:
            def account_id(self, account):
                return account.get("id")

            def account_email(self, account):
                return account.get("email")

        account = {
            "id": "2320",
            "email": "plus@example.com",
            "credentials": {"plan_type": "plus"},
            "extra": {"codex_5h_window_minutes": 0, "codex_7d_window_minutes": 10_080},
        }
        usage = {
            "five_hour": {"window_minutes": 0},
            "seven_day": {
                "used_percent": 100.0,
                "window_minutes": 10_080,
                "reset_at": "2026-07-20T03:05:32+08:00",
                "window_stats": {"cost": 120.0},
            },
        }

        samples = _usage_limit_sample_updates([account], FakeSub2Api(), {"2320": usage})

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["window_key"], "seven_day")
        self.assertEqual(samples[0]["plan_cohort"], "plus")

    def test_calibration_uses_statistics_at_ten_samples(self) -> None:
        ten_samples = _limit_calibration("seven_day", [180.0] * 10, "team")
        eleven_samples = _limit_calibration("seven_day", [180.0] * 11, "team")
        monthly_default = _limit_calibration("monthly", [], "team")

        self.assertEqual(ten_samples["source"], "sigma")
        self.assertAlmostEqual(ten_samples["lower"], 180.0)
        self.assertAlmostEqual(ten_samples["upper"], 180.0)
        self.assertEqual(eleven_samples["source"], "sigma")
        self.assertAlmostEqual(eleven_samples["lower"], 180.0)
        self.assertAlmostEqual(eleven_samples["upper"], 180.0)
        self.assertAlmostEqual(monthly_default["lower"], 100.0)
        self.assertAlmostEqual(monthly_default["upper"], 300.0)

    def test_usage_limit_samples_keep_distinct_resets_per_account_window(self) -> None:
        asyncio.run(self._assert_usage_limit_samples_keep_distinct_resets_per_account_window())

    def test_usage_limit_calibrations_use_all_retained_samples(self) -> None:
        asyncio.run(self._assert_usage_limit_calibrations_use_all_retained_samples())

    async def _assert_usage_limit_calibrations_use_all_retained_samples(self) -> None:
        original_sessionmaker = usage_estimate.AsyncSessionLocal
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "calibrations.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
            usage_estimate.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

                samples = [
                    {
                        "account_key": f"id:old-{index}",
                        "email": f"old-{index}@example.com",
                        "sub2api_account_id": f"old-{index}",
                        "plan_cohort": "team",
                        "window_key": "seven_day",
                        "reset_key": f"reset:2099-01-{index + 1:02d}T00:00:00Z",
                        "reset_at": f"2099-01-{index + 1:02d}T00:00:00Z",
                        "observed_limit": 180.0,
                        "raw_spent": 180.0,
                        "used_percent": 100.0,
                    }
                    for index in range(10)
                ]

                await _save_usage_limit_samples(samples)

                calibrations = await _load_usage_limit_calibrations()
                team_calibration = calibrations["seven_day"]["team"]

                self.assertEqual(team_calibration["source"], "sigma")
                self.assertEqual(team_calibration["sample_count"], 10)
                self.assertAlmostEqual(team_calibration["lower"], 180.0)
                self.assertAlmostEqual(team_calibration["upper"], 180.0)
            finally:
                usage_estimate.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()

    async def _assert_usage_limit_samples_keep_distinct_resets_per_account_window(self) -> None:
        original_sessionmaker = usage_estimate.AsyncSessionLocal
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "samples.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
            usage_estimate.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

                first_sample = {
                    "account_key": "id:1",
                    "email": "example@example.com",
                    "sub2api_account_id": "1",
                    "plan_cohort": "plus",
                    "window_key": "five_hour",
                    "reset_key": "reset:2099-01-01T05:00:00Z",
                    "reset_at": "2099-01-01T05:00:00Z",
                    "observed_limit": 20.0,
                    "raw_spent": 20.0,
                    "used_percent": 100.0,
                }
                updated_first_sample = {**first_sample, "observed_limit": 21.0, "raw_spent": 21.0}
                second_sample = {
                    **first_sample,
                    "reset_key": "reset:2099-01-01T10:00:00Z",
                    "reset_at": "2099-01-01T10:00:00Z",
                    "observed_limit": 22.0,
                    "raw_spent": 22.0,
                }

                await _save_usage_limit_samples([first_sample])
                await _save_usage_limit_samples([updated_first_sample])
                await _save_usage_limit_samples([second_sample])

                async with usage_estimate.AsyncSessionLocal() as db:
                    result = await db.execute(select(UsageLimitSample).order_by(UsageLimitSample.reset_at))
                    rows = list(result.scalars().all())

                self.assertEqual(len(rows), 2)
                self.assertEqual([row.reset_at for row in rows], ["2099-01-01T05:00:00Z", "2099-01-01T10:00:00Z"])
                self.assertEqual([row.observed_limit for row in rows], [21.0, 22.0])
            finally:
                usage_estimate.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()

    def test_long_window_only_account_counts_in_five_hour_and_long_window_aggregates(self) -> None:
        for window_kind in ("seven_day", "monthly"):
            with self.subTest(window_kind=window_kind):
                rows = [
                    {
                        "usage_estimate_enabled": True,
                        "deactive": False,
                        "error": False,
                        "usage_error": None,
                        "rate_limited": False,
                        "five_hour": {
                            "window_kind": "none",
                            "estimate_spent": None,
                            "estimated_limit": None,
                            "remaining": None,
                        },
                        "seven_day": {
                            "window_kind": window_kind,
                            "estimate_spent": 20.0,
                            "estimated_limit": 100.0,
                            "remaining": 80.0,
                        },
                    }
                ]

                five_hour = _aggregate_window(rows, "five_hour")
                seven_day = _aggregate_window(rows, "seven_day")

                self.assertEqual(five_hour["enabled_account_count"], 1)
                self.assertEqual(five_hour["estimable_accounts"], 1)
                self.assertAlmostEqual(five_hour["spent"], 20.0)
                self.assertAlmostEqual(five_hour["estimated_limit"], 100.0)
                self.assertAlmostEqual(five_hour["remaining"], 80.0)
                self.assertAlmostEqual(five_hour["remaining_percent"], 80.0)
                self.assertAlmostEqual(five_hour["used_percent"], 20.0)
                self.assertAlmostEqual(seven_day["estimated_limit"], 100.0)

    def test_aggregate_uses_account_availability_for_five_hour_but_long_window_for_seven_day(self) -> None:
        rows = [
            {
                "usage_estimate_enabled": True,
                "deactive": False,
                "error": False,
                "usage_error": None,
                "rate_limited": True,
                "five_hour": {
                    "window_kind": "five_hour",
                    "rate_limited": True,
                    "estimate_spent": 25.0,
                    "estimated_limit": 25.0,
                    "remaining": 0.0,
                },
                "seven_day": {
                    "window_kind": "seven_day",
                    "rate_limited": False,
                    "estimate_spent": 20.0,
                    "estimated_limit": 100.0,
                    "remaining": 80.0,
                },
            },
            {
                "usage_estimate_enabled": True,
                "deactive": False,
                "error": False,
                "usage_error": None,
                "rate_limited": True,
                "five_hour": {
                    "window_kind": "five_hour",
                    "rate_limited": False,
                    "estimate_spent": 5.0,
                    "estimated_limit": 25.0,
                    "remaining": 20.0,
                },
                "seven_day": {
                    "window_kind": "seven_day",
                    "rate_limited": True,
                    "estimate_spent": 100.0,
                    "estimated_limit": 100.0,
                    "remaining": 0.0,
                },
            },
        ]

        five_hour = _aggregate_window(rows, "five_hour")
        seven_day = _aggregate_window(rows, "seven_day")

        self.assertEqual(five_hour["enabled_account_count"], 0)
        self.assertEqual(five_hour["estimable_accounts"], 0)
        self.assertAlmostEqual(five_hour["spent"], 0.0)
        self.assertIsNone(five_hour["estimated_limit"])
        self.assertAlmostEqual(five_hour["remaining"], 0.0)
        self.assertEqual(seven_day["enabled_account_count"], 1)
        self.assertEqual(seven_day["estimable_accounts"], 1)
        self.assertAlmostEqual(seven_day["spent"], 20.0)
        self.assertAlmostEqual(seven_day["estimated_limit"], 100.0)
        self.assertAlmostEqual(seven_day["remaining"], 80.0)

    def test_monthly_only_usage_is_reused_for_five_hour_and_seven_day(self) -> None:
        account = {
            "id": "1",
            "email": "team@example.com",
            "status": "active",
            "schedulable": True,
            "credentials": {"plan_type": "team"},
            "extra": {"codex_5h_window_minutes": 0},
        }
        usage = {
            "monthly": {
                "used_percent": 20.0,
                "remaining_seconds": 1_000_000,
                "reset_at": "2099-07-01T00:00:00Z",
                "window_stats": {"cost": 0.0},
            }
        }
        limit_calibrations = {
            "monthly": {
                "team": _limit_calibration("monthly", [], "team"),
                "unknown": _limit_calibration("monthly", [], "unknown"),
            }
        }

        five_hour = _window_estimate(
            account=account,
            usage=usage,
            window_key="five_hour",
            account_key="id:1",
            account_id="1",
            email="team@example.com",
            plan_cohort="team",
            usage_states={},
            limit_calibrations=limit_calibrations,
        )
        seven_day = _window_estimate(
            account=account,
            usage=usage,
            window_key="seven_day",
            account_key="id:1",
            account_id="1",
            email="team@example.com",
            plan_cohort="team",
            usage_states={},
            limit_calibrations=limit_calibrations,
        )

        for window in (five_hour, seven_day):
            self.assertEqual(window["window_kind"], "monthly")
            self.assertIsNone(window["raw_spent"])
            self.assertAlmostEqual(window["estimated_limit"], 200.0)
            self.assertIsNone(window["estimate_spent"])
            self.assertAlmostEqual(window["remaining"], 160.0)

    def test_monthly_only_usage_uses_sub2api_limit_remaining_amounts(self) -> None:
        account = {
            "id": "1",
            "email": "ctf1du13uabx1@gptteam.ikun.edu.rs",
            "status": "active",
            "schedulable": True,
            "extra": {"codex_5h_window_minutes": 0},
        }
        usage = {
            "seven_day": {
                "used_percent": 20.0,
                "remaining_seconds": 1_000_000,
                "reset_at": "2099-07-01T00:00:00Z",
                "window_stats": {"cost": 0.0, "limit": 300.0, "remaining": 240.0},
            }
        }
        limit_calibrations = {
            "monthly": {
                "team": _limit_calibration("monthly", [], "team"),
                "unknown": _limit_calibration("monthly", [], "unknown"),
            }
        }

        five_hour = _window_estimate(
            account=account,
            usage=usage,
            window_key="five_hour",
            account_key="id:1",
            account_id="1",
            email="ctf1du13uabx1@gptteam.ikun.edu.rs",
            plan_cohort="team",
            usage_states={},
            limit_calibrations=limit_calibrations,
        )
        seven_day = _window_estimate(
            account=account,
            usage=usage,
            window_key="seven_day",
            account_key="id:1",
            account_id="1",
            email="ctf1du13uabx1@gptteam.ikun.edu.rs",
            plan_cohort="team",
            usage_states={},
            limit_calibrations=limit_calibrations,
        )

        for window in (five_hour, seven_day):
            self.assertEqual(window["window_kind"], "monthly")
            self.assertAlmostEqual(window["raw_spent"], 60.0)
            self.assertAlmostEqual(window["estimated_limit"], 300.0)
            self.assertAlmostEqual(window["estimate_spent"], 60.0)
            self.assertAlmostEqual(window["remaining"], 240.0)

    def test_monthly_only_usage_reverses_from_five_hour_cost_when_monthly_cost_is_zero(self) -> None:
        account = {
            "id": "961",
            "email": "ctf1du13uabx1@gptteam.ikun.edu.rs",
            "status": "active",
            "schedulable": True,
            "credentials": {"plan_type": "team"},
            "extra": {"codex_5h_window_minutes": 0, "codex_7d_window_minutes": 43800},
        }
        usage = {
            "five_hour": {
                "utilization": 0,
                "resets_at": "2026-06-07T10:22:55Z",
                "remaining_seconds": 0,
                "window_stats": {"requests": 782, "cost": 74.54112675},
            },
            "seven_day": {
                "utilization": 64,
                "resets_at": "2026-07-07T04:24:56Z",
                "remaining_seconds": 2570365,
                "window_stats": {"requests": 0, "cost": 0},
            },
        }
        limit_calibrations = {
            "monthly": {
                "team": _limit_calibration("monthly", [], "team"),
                "unknown": _limit_calibration("monthly", [], "unknown"),
            }
        }

        five_hour = _window_estimate(
            account=account,
            usage=usage,
            window_key="five_hour",
            account_key="id:961",
            account_id="961",
            email="ctf1du13uabx1@gptteam.ikun.edu.rs",
            plan_cohort="team",
            usage_states={},
            limit_calibrations=limit_calibrations,
        )
        seven_day = _window_estimate(
            account=account,
            usage=usage,
            window_key="seven_day",
            account_key="id:961",
            account_id="961",
            email="ctf1du13uabx1@gptteam.ikun.edu.rs",
            plan_cohort="team",
            usage_states={},
            limit_calibrations=limit_calibrations,
        )

        expected_limit = 74.54112675 / 0.64
        for window in (five_hour, seven_day):
            self.assertEqual(window["window_kind"], "monthly")
            self.assertAlmostEqual(window["raw_spent"], 74.54112675)
            self.assertAlmostEqual(window["estimated_limit"], expected_limit)
            self.assertAlmostEqual(window["estimate_spent"], 74.54112675)
            self.assertAlmostEqual(window["remaining"], expected_limit - 74.54112675)

    def test_monthly_only_team_limit_sample_is_collected_separately(self) -> None:
        class FakeSub2Api:
            def account_id(self, account):
                return account.get("id")

            def account_email(self, account):
                return account.get("email")

        account = {
            "id": "1",
            "email": "team@example.com",
            "credentials": {"plan_type": "team"},
        }
        usage = {
            "monthly": {
                "used_percent": 100.0,
                "reset_at": "2099-07-01T00:00:00Z",
                "window_stats": {"cost": 250.0},
            }
        }

        samples = _usage_limit_sample_updates([account], FakeSub2Api(), {"1": usage})

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["window_key"], "monthly")
        self.assertEqual(samples[0]["plan_cohort"], "team")
        self.assertAlmostEqual(samples[0]["observed_limit"], 250.0)


if __name__ == "__main__":
    unittest.main()
