from __future__ import annotations

import unittest

from app.core.subscription_types import (
    normalize_subscription_type,
    normalize_usage_limit_ranges,
    subscription_type_label,
    usage_limit_bounds,
)


class SubscriptionTypesTests(unittest.TestCase):
    def test_k12_aliases_are_canonical(self) -> None:
        for value in ("k12", "K-12", "ChatGPTK12Plan", "chatgpt_k12_plan"):
            self.assertEqual(normalize_subscription_type(value), "k12")
            self.assertEqual(subscription_type_label(value), "K12")

    def test_future_subscription_type_keeps_stable_identity(self) -> None:
        self.assertEqual(normalize_subscription_type("ChatGPT Enterprise Edu Plan"), "enterprise-edu")
        self.assertEqual(subscription_type_label("ChatGPT Enterprise Edu Plan"), "Enterprise Edu")

    def test_non_plan_sentinels_remain_unknown(self) -> None:
        self.assertEqual(normalize_subscription_type("active"), "unknown")
        self.assertEqual(normalize_subscription_type(None), "unknown")

    def test_custom_ranges_merge_with_unknown_fallback(self) -> None:
        ranges = normalize_usage_limit_ranges(
            {
                "k12": {
                    "five_hour": {"lower": 30, "upper": 45},
                    "seven_day": {"lower": 200, "upper": 260},
                    "monthly": {"lower": 500, "upper": 700},
                },
                "enterprise": {"seven_day": {"lower": 300, "upper": 400}},
            }
        )

        self.assertEqual(usage_limit_bounds(ranges, "five_hour", "k12"), (30.0, 45.0))
        self.assertEqual(usage_limit_bounds(ranges, "monthly", "k12"), (800.0, 1040.0))
        self.assertEqual(usage_limit_bounds(ranges, "seven_day", "enterprise"), (300.0, 400.0))
        self.assertEqual(usage_limit_bounds(ranges, "monthly", "enterprise"), (1200.0, 1600.0))
        self.assertEqual(usage_limit_bounds(ranges, "five_hour", "enterprise"), (15.0, 25.0))
        self.assertEqual(usage_limit_bounds(ranges, "monthly", "future-plan"), (400.0, 560.0))

    def test_team_monthly_range_remains_independently_configurable(self) -> None:
        ranges = normalize_usage_limit_ranges(
            {
                "team": {
                    "seven_day": {"lower": 200, "upper": 260},
                    "monthly": {"lower": 500, "upper": 700},
                }
            }
        )

        self.assertEqual(usage_limit_bounds(ranges, "monthly", "team"), (500.0, 700.0))


if __name__ == "__main__":
    unittest.main()
