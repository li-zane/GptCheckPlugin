from __future__ import annotations

import unittest

from app.services.sub2api import (
    _parse_account_daily_costs,
    _parse_account_daily_stats,
    _parse_account_today_costs,
    _parse_account_today_stats,
)


class Sub2ApiTodayCostsTests(unittest.TestCase):
    def test_accepts_supported_cost_field_aliases(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "stats": {
                    "1": {"today_actual_cost": "1.25"},
                    "2": {"accountId": 2, "todayActualCost": 2.5},
                    "3": {"actual_cost": 3},
                    "4": {"actualCost": 4.75},
                    "5": {"cost": 5},
                    "6": {"today_actual_cost": None, "cost": 6.5},
                    "7": {"cost": 7, "user_cost": 9},
                }
            },
        }

        self.assertEqual(
            _parse_account_today_costs(payload, {1, 2, 3, 4, 5, 6, 7}),
            {
                1: 1.25,
                2: 2.5,
                3: 3.0,
                4: 4.75,
                5: 5.0,
                6: 6.5,
                7: 7.0,
            },
        )
        self.assertEqual(
            _parse_account_today_stats(payload, {1, 2, 3, 4, 5, 6, 7}),
            {
                1: {"cost": 1.25, "user_cost": 1.25},
                2: {"cost": 2.5, "user_cost": 2.5},
                3: {"cost": 3.0, "user_cost": 3.0},
                4: {"cost": 4.75, "user_cost": 4.75},
                5: {"cost": 5.0, "user_cost": None},
                6: {"cost": 6.5, "user_cost": None},
                7: {"cost": 7.0, "user_cost": 9.0},
            },
        )

    def test_parses_historical_actual_costs_by_date(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "history": [
                    {"date": "2026-07-27", "actual_cost": "4.5"},
                    {"date": "2026-07-28T00:00:00Z", "actualCost": 6},
                    {"date": "2026-07-26", "total_cost": 8, "total_user_cost": 10},
                    {"date": "invalid", "actual_cost": 9},
                    {"date": "2026-07-29", "actual_cost": -1},
                ]
            },
        }

        self.assertEqual(
            {item.isoformat(): value for item, value in _parse_account_daily_costs(payload).items()},
            {"2026-07-26": 8.0, "2026-07-27": 4.5, "2026-07-28": 6.0},
        )
        self.assertEqual(
            {
                item.isoformat(): value
                for item, value in _parse_account_daily_stats(payload).items()
            },
            {
                "2026-07-26": {"cost": 8.0, "user_cost": 10.0},
                "2026-07-27": {"cost": 4.5, "user_cost": 4.5},
                "2026-07-28": {"cost": 6.0, "user_cost": 6.0},
            },
        )


if __name__ == "__main__":
    unittest.main()
