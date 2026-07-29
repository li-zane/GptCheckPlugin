from __future__ import annotations

import unittest

from app.services.sub2api import _parse_account_daily_costs, _parse_account_today_costs


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
                }
            },
        }

        self.assertEqual(
            _parse_account_today_costs(payload, {1, 2, 3, 4, 5, 6}),
            {
                1: 1.25,
                2: 2.5,
                3: 3.0,
                4: 4.75,
            },
        )

    def test_parses_historical_actual_costs_by_date(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "history": [
                    {"date": "2026-07-27", "actual_cost": "4.5"},
                    {"date": "2026-07-28T00:00:00Z", "actualCost": 6},
                    {"date": "2026-07-26", "cost": 8},
                    {"date": "invalid", "actual_cost": 9},
                    {"date": "2026-07-29", "actual_cost": -1},
                ]
            },
        }

        self.assertEqual(
            {item.isoformat(): value for item, value in _parse_account_daily_costs(payload).items()},
            {"2026-07-27": 4.5, "2026-07-28": 6.0},
        )


if __name__ == "__main__":
    unittest.main()
