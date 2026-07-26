from __future__ import annotations

import unittest

from app.services.sub2api import _parse_account_today_costs


class Sub2ApiTodayCostsTests(unittest.TestCase):
    def test_accepts_supported_cost_field_aliases(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "stats": {
                    "1": {"today_actual_cost": "1.25"},
                    "2": {"accountId": 2, "todayActualCost": 2.5},
                    "3": {"cost": 3},
                    "4": {"today_cost": 4.75},
                    "5": {"todayCost": 5},
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
                5: 5.0,
                6: 6.5,
            },
        )


if __name__ == "__main__":
    unittest.main()
