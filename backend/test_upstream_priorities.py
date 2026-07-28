from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.upstream_accounts import router
from app.core.database import (
    Base,
    _migrate_upstream_priority_intervals,
    get_db,
)
from app.core.security import require_admin
from app.models import UpstreamAccountConfig, UpstreamPriorityInterval
from app.schemas import (
    PriorityIntervalCreate,
    PriorityIntervalAssignment,
    PriorityIntervalOut,
    PriorityRebalanceOut,
    PriorityTieMoveRequest,
)
from app.services.sub2api import Sub2ApiClient
from app.services.upstream_accounts import UpstreamAccountService, UpstreamAccountServiceError
from app.services.upstream_priorities import (
    UpstreamPriorityService,
    _tie_multiplier_key,
    allocate_interval_priorities,
    composite_multiplier,
    get_upstream_priority_service,
)


class FakePrioritySub2Api(Sub2ApiClient):
    def __init__(self) -> None:
        super().__init__()
        self.accounts: list[dict] = []
        self.priority_update_calls: list[tuple[list[int], int]] = []
        self.update_started: asyncio.Event | None = None
        self.allow_update: asyncio.Event | None = None

    async def list_api_key_accounts(self) -> list[dict]:
        return [dict(account) for account in self.accounts]

    async def get_account_by_id(self, account_id: str | int, **_kwargs) -> dict | None:
        parsed_id = int(account_id)
        return next(
            (dict(account) for account in self.accounts if int(account["id"]) == parsed_id),
            None,
        )

    async def update_account_priorities(self, account_ids: list[int], priority: int) -> None:
        self.priority_update_calls.append((list(account_ids), priority))
        if self.update_started is not None:
            self.update_started.set()
        if self.allow_update is not None:
            await self.allow_update.wait()
        for account in self.accounts:
            if int(account["id"]) in account_ids:
                account["priority"] = priority

    async def delete_account(self, account: dict | str) -> bool:
        account_id = int(account if isinstance(account, str) else account["id"])
        before = len(self.accounts)
        self.accounts = [item for item in self.accounts if int(item["id"]) != account_id]
        return len(self.accounts) != before


def remote_account(account_id: int, *, priority: int = 50) -> dict:
    return {
        "id": account_id,
        "name": f"account-{account_id}",
        "platform": "openai",
        "type": "apikey",
        "status": "active",
        "priority": priority,
        "rate_multiplier": 1.0,
        "created_at": f"2026-07-01T00:00:{account_id % 60:02}Z",
        "credentials": {"base_url": "https://upstream.example/v1"},
    }


class PriorityAllocatorTests(unittest.TestCase):
    @staticmethod
    def _config(account_id: int, multiplier: float | None) -> UpstreamAccountConfig:
        config = UpstreamAccountConfig(
            sub2api_account_id=account_id,
            priority_sync_status="unassigned",
        )
        config.effective_group_multiplier = multiplier
        config.effective_recharge_multiplier = 1.0 if multiplier is not None else None
        return config

    def test_allocator_uses_full_span_and_reports_capacity_collisions(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=40,
            end_priority=70,
            step=2,
            allocation_strategy="fixed_step",
        )
        sixteen = [self._config(index + 1, float(index + 1)) for index in range(16)]
        assignments, effective_step = allocate_interval_priorities(interval, sixteen)
        self.assertEqual(effective_step, 1)
        self.assertEqual(assignments[1], 40)
        self.assertEqual(assignments[2], 41)
        self.assertEqual(assignments[8], 47)
        self.assertEqual(assignments[16], 55)

        thirty_one = [self._config(index + 1, float(index + 1)) for index in range(31)]
        assignments, effective_step = allocate_interval_priorities(interval, thirty_one)
        self.assertEqual(effective_step, 0)
        self.assertEqual(assignments[1], 40)
        self.assertEqual(assignments[2], 41)
        self.assertEqual(assignments[31], 69)

    def test_allocator_uses_composite_multiplier_and_excludes_unknown_values(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=10,
            end_priority=20,
            step=3,
        )
        expensive = self._config(7, 2.0)
        cheap = self._config(8, 0.5)
        unknown = self._config(9, None)

        assignments, effective_step = allocate_interval_priorities(
            interval,
            [expensive, unknown, cheap],
        )

        self.assertEqual(effective_step, 1)
        self.assertEqual(assignments, {8: 10, 7: 19})
        self.assertNotIn(9, assignments)

    def test_allocator_maps_inverse_cost_efficiency_onto_the_interval(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=40,
            end_priority=70,
            step=2,
        )
        cheap = self._config(1, 0.1)
        near_cheap = self._config(2, 0.2)
        expensive = self._config(3, 1.0)

        assignments, effective_step = allocate_interval_priorities(
            interval,
            [expensive, near_cheap, cheap],
        )

        self.assertEqual(effective_step, 1)
        self.assertEqual(assignments, {1: 40, 2: 50, 3: 69})

    def test_cost_curve_ignores_configured_step_and_uses_unit_tie_spacing(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=40,
            end_priority=70,
            step=8,
            allocation_strategy="cost_optimized",
        )
        alpha = self._config(1, 0.1)
        beta = self._config(2, 0.1)
        expensive = self._config(3, 1.0)

        assignments, effective_step = allocate_interval_priorities(
            interval,
            [expensive, beta, alpha],
        )

        self.assertEqual(effective_step, 1)
        self.assertEqual(assignments, {1: 40, 2: 41, 3: 69})

    def test_cost_curve_spreads_unavoidable_tie_collisions_across_interval(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="small",
            start_priority=40,
            end_priority=43,
            step=9,
            allocation_strategy="cost_optimized",
        )
        configs = [self._config(index, 0.1) for index in range(1, 5)]

        assignments, effective_step = allocate_interval_priorities(interval, configs)

        self.assertEqual(effective_step, 0)
        self.assertEqual(assignments, {1: 40, 2: 40, 3: 41, 4: 42})

    def test_shared_priority_curve_keeps_account_weighted_geometric_median(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=40,
            end_priority=70,
            step=2,
        )
        configs = [
            self._config(1, 0.1),
            self._config(2, 0.1),
            self._config(3, 0.1),
            self._config(4, 0.2),
            self._config(5, 1.0),
        ]

        assignments, effective_step = allocate_interval_priorities(
            interval,
            configs,
            share_same_composite_multiplier=True,
        )

        self.assertEqual(effective_step, 1)
        self.assertEqual(assignments, {1: 40, 2: 40, 3: 40, 4: 52, 5: 69})

    def test_allocator_sorts_equal_multipliers_by_name_then_persisted_override(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=40,
            end_priority=70,
            step=2,
        )
        alpha = self._config(8, 0.1)
        alpha.remote_name = "Alpha"
        beta = self._config(7, 0.1)
        beta.remote_name = "beta"

        assignments, _ = allocate_interval_priorities(interval, [beta, alpha])
        self.assertEqual(assignments, {8: 40, 7: 41})

        alpha.priority_tiebreak_order = 1
        alpha.priority_tiebreak_multiplier = 0.1
        beta.priority_tiebreak_order = 0
        beta.priority_tiebreak_multiplier = 0.1
        assignments, _ = allocate_interval_priorities(interval, [alpha, beta])
        self.assertEqual(assignments, {7: 40, 8: 41})

        beta.priority_tiebreak_multiplier = 0.2
        assignments, _ = allocate_interval_priorities(interval, [beta, alpha])
        self.assertEqual(assignments, {8: 40, 7: 41})

    def test_allocator_can_share_priority_across_equal_multiplier_accounts(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=40,
            end_priority=70,
            step=2,
        )
        alpha = self._config(7, 0.1)
        beta = self._config(8, 0.1)
        expensive = self._config(9, 0.2)

        assignments, effective_step = allocate_interval_priorities(
            interval,
            [expensive, beta, alpha],
            share_same_composite_multiplier=True,
        )

        self.assertEqual(effective_step, 1)
        self.assertEqual(assignments, {7: 40, 8: 40, 9: 69})

    def test_shared_multiplier_groups_determine_effective_step(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="small",
            start_priority=40,
            end_priority=44,
            step=3,
        )
        configs = [
            self._config(7, 0.1),
            self._config(8, 0.1),
            self._config(9, 0.2),
            self._config(10, 0.2),
        ]

        assignments, effective_step = allocate_interval_priorities(
            interval,
            configs,
            share_same_composite_multiplier=True,
        )

        self.assertEqual(effective_step, 1)
        self.assertEqual(assignments, {7: 40, 8: 40, 9: 43, 10: 43})

    def test_allocator_keeps_high_precision_tiebreak_after_float_storage(self) -> None:
        interval = UpstreamPriorityInterval(
            id=1,
            name="main",
            start_priority=40,
            end_priority=70,
            step=2,
        )
        alpha = self._config(8, 1.23456789012345)
        alpha.effective_recharge_multiplier = 9.87654321098765
        alpha.remote_name = "Alpha"
        beta = self._config(7, 1.23456789012345)
        beta.effective_recharge_multiplier = 9.87654321098765
        beta.remote_name = "Beta"
        alpha.priority_tiebreak_order = 1
        alpha.priority_tiebreak_multiplier = float(composite_multiplier(alpha))
        beta.priority_tiebreak_order = 0
        beta.priority_tiebreak_multiplier = float(composite_multiplier(beta))

        assignments, _ = allocate_interval_priorities(interval, [alpha, beta])

        self.assertEqual(assignments, {7: 40, 8: 41})
        self.assertEqual(
            _tie_multiplier_key(Decimal("1.00000000000025")),
            Decimal("1.0000000000002"),
        )
        self.assertEqual(_tie_multiplier_key(Decimal("5e-14")), Decimal("0E-13"))

        large_alpha = self._config(10, 195.3507753057386)
        large_alpha.effective_recharge_multiplier = 361.49042130810847
        large_alpha.remote_name = "Alpha"
        large_beta = self._config(11, 195.3507753057386)
        large_beta.effective_recharge_multiplier = 361.49042130810847
        large_beta.remote_name = "Beta"
        stored_large_multiplier = float(composite_multiplier(large_alpha))
        large_alpha.priority_tiebreak_order = 1
        large_alpha.priority_tiebreak_multiplier = stored_large_multiplier
        large_beta.priority_tiebreak_order = 0
        large_beta.priority_tiebreak_multiplier = stored_large_multiplier

        large_assignments, _ = allocate_interval_priorities(
            interval,
            [large_alpha, large_beta],
        )

        self.assertEqual(large_assignments, {11: 40, 10: 41})

class UpstreamPriorityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.sub2api = FakePrioritySub2Api()
        self.accounts = UpstreamAccountService(self.sub2api)
        self.service = UpstreamPriorityService(accounts=self.accounts)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    async def _interval(
        self,
        *,
        name: str = "main",
        start: int = 40,
        end: int = 70,
        step: int = 2,
    ) -> PriorityIntervalOut:
        return await self.service.create_interval(
            self.db,
            PriorityIntervalCreate(
                name=name,
                start_priority=start,
                end_priority=end,
                step=step,
            ),
        )

    async def _add_config(
        self,
        account_id: int,
        interval_id: int,
        *,
        group: float | None,
        recharge: float | None = 1.0,
        priority: int = 50,
        name: str | None = None,
    ) -> UpstreamAccountConfig:
        remote = remote_account(account_id, priority=priority)
        if name is not None:
            remote["name"] = name
        self.sub2api.accounts.append(remote)
        config = self.accounts._new_config(remote, account_id)
        config.priority_interval_id = interval_id
        config.effective_group_multiplier = group
        config.effective_recharge_multiplier = recharge
        self.db.add(config)
        await self.db.commit()
        return config

    async def test_equal_multiplier_priority_can_be_swapped_and_persists(self) -> None:
        interval = await self._interval()
        alpha = await self._add_config(7, interval.id, group=0.1, name="Alpha")
        beta = await self._add_config(8, interval.id, group=0.1, name="Beta")
        runtime = SimpleNamespace(
            get_priority_assign_disabled_api_key_accounts=AsyncMock(return_value=False),
            get_priority_share_same_composite_multiplier=AsyncMock(return_value=False),
        )

        with patch(
            "app.services.upstream_priorities.get_runtime_config_service",
            return_value=runtime,
        ):
            await self.service.rebalance(self.db)
            fingerprint = self.accounts._remote_identity_fingerprint(
                self.sub2api.accounts[0]
            )

            moved = await self.service.move_equal_multiplier_priority(
                self.db,
                7,
                PriorityTieMoveRequest(
                    direction="up",
                    expected_identity_fingerprint=fingerprint,
                ),
            )

        self.assertEqual(moved.failed, 0)
        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 41, 8: 40})
        await self.db.refresh(alpha)
        await self.db.refresh(beta)
        self.assertEqual((alpha.priority_tiebreak_order, beta.priority_tiebreak_order), (1, 0))

        with patch(
            "app.services.upstream_priorities.get_runtime_config_service",
            return_value=runtime,
        ):
            await self.service.rebalance(self.db)
        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 41, 8: 40})

    async def test_shared_multiplier_mode_rebalances_to_one_priority_and_rejects_tie_move(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=0.1, name="Alpha")
        await self._add_config(8, interval.id, group=0.1, name="Beta")
        await self._add_config(9, interval.id, group=0.2, name="Gamma")
        runtime = SimpleNamespace(
            get_priority_assign_disabled_api_key_accounts=AsyncMock(return_value=False),
            get_priority_share_same_composite_multiplier=AsyncMock(return_value=True),
        )

        with patch(
            "app.services.upstream_priorities.get_runtime_config_service",
            return_value=runtime,
        ):
            result = await self.service.rebalance(self.db)
            with self.assertRaises(UpstreamAccountServiceError) as raised:
                await self.service.move_equal_multiplier_priority(
                    self.db,
                    7,
                    PriorityTieMoveRequest(
                        direction="up",
                        expected_identity_fingerprint=(
                            self.accounts._remote_identity_fingerprint(
                                self.sub2api.accounts[0]
                            )
                        ),
                    ),
                )

        self.assertEqual((result.updated, result.failed), (3, 0))
        self.assertEqual(raised.exception.status_code, 409)
        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 40, 8: 40, 9: 69})
        listed = await self.service.list_intervals(self.db)
        self.assertEqual(listed[0].effective_step, 1)

    async def test_intervals_allow_overlap_and_adjacent_bounds(self) -> None:
        await self._interval(start=40, end=70)
        overlap = await self._interval(name="overlap", start=69, end=90)
        self.assertEqual((overlap.start_priority, overlap.end_priority), (69, 90))

        adjacent = await self._interval(name="adjacent", start=70, end=90)
        self.assertEqual((adjacent.start_priority, adjacent.end_priority), (70, 90))

    async def test_rebalance_sorts_by_composite_and_verifies_remote_priorities(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=2.0)
        await self._add_config(8, interval.id, group=0.5)
        await self._add_config(9, interval.id, group=1.0)

        result = await self.service.rebalance(self.db)

        self.assertEqual((result.updated, result.failed), (3, 0))
        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 69, 8: 40, 9: 55})
        stored = (await self.db.execute(select(UpstreamAccountConfig))).scalars().all()
        self.assertTrue(all(item.priority_sync_status == "in_sync" for item in stored))
        listed = await self.accounts.list_accounts(self.db)
        self.assertEqual([item.sub2api_account_id for item in listed], [8, 9, 7])

    async def test_rebalance_reuses_supplied_snapshot_when_priorities_are_unchanged(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=1.0, priority=40)
        await self._add_config(8, interval.id, group=2.0, priority=69)
        remote_by_id = {
            int(account["id"]): dict(account)
            for account in self.sub2api.accounts
        }

        with patch.object(
            self.accounts,
            "_remote_accounts",
            new=AsyncMock(wraps=self.accounts._remote_accounts),
        ) as list_accounts:
            result = await self.service.rebalance(
                self.db,
                remote_by_id=remote_by_id,
            )

        self.assertEqual((result.updated, result.unchanged, result.failed), (0, 2, 0))
        list_accounts.assert_not_awaited()

    async def test_priority_write_readback_refreshes_supplied_snapshot(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=1.0, priority=50)
        remote_by_id = {7: dict(self.sub2api.accounts[0])}

        with patch.object(
            self.accounts,
            "_remote_accounts",
            new=AsyncMock(wraps=self.accounts._remote_accounts),
        ) as list_accounts:
            result = await self.service.rebalance(
                self.db,
                remote_by_id=remote_by_id,
            )

        self.assertEqual((result.updated, result.failed), (1, 0))
        list_accounts.assert_awaited_once_with()
        self.assertEqual(remote_by_id[7]["priority"], 40)

    async def test_disabled_account_priority_respects_global_and_account_override(self) -> None:
        interval = await self._interval()
        disabled = await self._add_config(7, interval.id, group=0.5)
        await self._add_config(8, interval.id, group=1.0)
        self.sub2api.accounts[0]["schedulable"] = False
        runtime = SimpleNamespace(
            get_priority_assign_disabled_api_key_accounts=AsyncMock(return_value=False)
        )

        with patch(
            "app.services.upstream_priorities.get_runtime_config_service",
            return_value=runtime,
        ):
            await self.service.rebalance(self.db)
            await self.db.refresh(disabled)
            self.assertEqual(disabled.priority_sync_status, "disabled_excluded")
            self.assertIsNone(disabled.desired_priority)
            self.assertEqual(self.sub2api.accounts[0]["priority"], 50)

            disabled.priority_assignment_when_disabled = True
            await self.db.commit()
            await self.service.rebalance(self.db)

        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 40, 8: 69})

    async def test_subset_rebalance_updates_the_complete_affected_interval(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=2.0, priority=90)
        await self._add_config(8, interval.id, group=0.5, priority=91)
        unrelated = await self._interval(
            name="unrelated",
            start=80,
            end=90,
            step=2,
        )
        await self._add_config(9, unrelated.id, group=1.0, priority=77)

        result = await self.service.rebalance(self.db, account_ids={7})

        priorities = {
            int(account["id"]): int(account["priority"])
            for account in self.sub2api.accounts
        }
        self.assertEqual((result.considered, result.updated, result.failed), (2, 2, 0))
        self.assertEqual(priorities, {7: 69, 8: 40, 9: 77})

    async def test_unknown_multiplier_is_not_written_and_does_not_consume_capacity(self) -> None:
        interval = await self._interval(step=5)
        await self._add_config(7, interval.id, group=1.0, priority=99)
        await self._add_config(8, interval.id, group=None, recharge=None, priority=88)

        result = await self.service.rebalance(self.db)

        self.assertEqual((result.updated, result.failed), (1, 0))
        self.assertEqual(self.sub2api.accounts[0]["priority"], 40)
        self.assertEqual(self.sub2api.accounts[1]["priority"], 88)
        configs = {
            item.sub2api_account_id: item
            for item in (await self.db.execute(select(UpstreamAccountConfig))).scalars().all()
        }
        self.assertEqual(configs[8].priority_sync_status, "multiplier_unavailable")
        self.assertIsNone(configs[8].desired_priority)
        listed = await self.service.list_intervals(self.db)
        self.assertEqual(listed[0].account_count, 2)
        self.assertEqual(listed[0].effective_step, 1)

    async def test_delete_interval_only_unbinds_and_preserves_remote_priority(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=1.0, priority=63)
        self.sub2api.priority_update_calls.clear()

        removed = await self.service.delete_interval(self.db, interval.id)

        self.assertTrue(removed)
        self.assertEqual(self.sub2api.accounts[0]["priority"], 63)
        self.assertEqual(self.sub2api.priority_update_calls, [])
        config = (await self.db.execute(select(UpstreamAccountConfig))).scalar_one()
        self.assertIsNone(config.priority_interval_id)
        self.assertEqual(config.priority_sync_status, "unassigned")

    async def test_unassign_preserves_that_account_and_rebalances_old_interval(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=0.5, priority=90)
        await self._add_config(8, interval.id, group=1.0, priority=91)
        await self.service.rebalance(self.db)
        fingerprint = self.accounts._remote_identity_fingerprint(self.sub2api.accounts[0])

        account = await self.service.assign_interval(
            self.db,
            7,
            PriorityIntervalAssignment(
                expected_identity_fingerprint=fingerprint,
                priority_interval_id=None,
            ),
        )

        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 40, 8: 40})
        self.assertIsNone(account.priority_interval_id)
        self.assertEqual(account.priority_sync_status, "unassigned")

    async def test_assign_interval_can_explicitly_claim_an_unbound_legacy_config(self) -> None:
        interval = await self._interval()
        remote = remote_account(7, priority=90)
        self.sub2api.accounts.append(remote)
        config = self.accounts._new_config(remote, 7)
        config.remote_identity_fingerprint = None
        config.effective_group_multiplier = 0.5
        config.effective_recharge_multiplier = 1.0
        self.db.add(config)
        await self.db.commit()
        fingerprint = self.accounts._remote_identity_fingerprint(remote)

        with self.assertRaises(UpstreamAccountServiceError) as unconfirmed:
            await self.service.assign_interval(
                self.db,
                7,
                PriorityIntervalAssignment(
                    expected_identity_fingerprint=fingerprint,
                    priority_interval_id=interval.id,
                ),
            )
        self.assertEqual(unconfirmed.exception.status_code, 409)

        account = await self.service.assign_interval(
            self.db,
            7,
            PriorityIntervalAssignment(
                expected_identity_fingerprint=fingerprint,
                priority_interval_id=interval.id,
                confirm_identity_rebind=True,
            ),
        )

        await self.db.refresh(config)
        self.assertIsNotNone(config.remote_identity_fingerprint)
        self.assertEqual(config.priority_interval_id, interval.id)
        self.assertEqual(account.identity_binding_status, "bound")
        self.assertEqual(account.priority, 40)

    async def test_assign_interval_preserves_binding_after_remote_rename(self) -> None:
        interval = await self._interval()
        original = remote_account(7, priority=90)
        config = self.accounts._new_config(original, 7)
        config.effective_group_multiplier = 0.5
        config.effective_recharge_multiplier = 1.0
        self.db.add(config)
        await self.db.commit()
        replacement = {**original, "name": "replacement-account"}
        self.sub2api.accounts.append(replacement)

        account = await self.service.assign_interval(
            self.db,
            7,
            PriorityIntervalAssignment(
                expected_identity_fingerprint=(
                    self.accounts._remote_identity_fingerprint(replacement)
                ),
                priority_interval_id=interval.id,
            ),
        )

        await self.db.refresh(config)
        self.assertEqual(config.priority_interval_id, interval.id)
        self.assertEqual(config.remote_identity_fingerprint, self.accounts._remote_binding_fingerprint(replacement))
        self.assertEqual(account.identity_binding_status, "bound")
        self.assertEqual(account.priority, 40)

    async def test_local_and_remote_deletion_rebalance_remaining_accounts(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=0.5, priority=90)
        await self._add_config(8, interval.id, group=1.0, priority=91)
        await self.service.rebalance(self.db)
        first_fingerprint = self.accounts._remote_identity_fingerprint(self.sub2api.accounts[0])

        removed = await self.accounts.delete_account(self.db, 7, first_fingerprint)

        self.assertTrue(removed)
        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 40, 8: 40})

        config = self.accounts._new_config(self.sub2api.accounts[0], 7)
        config.priority_interval_id = interval.id
        config.effective_group_multiplier = 0.5
        config.effective_recharge_multiplier = 1.0
        self.db.add(config)
        await self.db.commit()
        await self.service.rebalance(self.db)
        first_fingerprint = self.accounts._remote_identity_fingerprint(self.sub2api.accounts[0])

        deleted = await self.accounts.delete_remote_account(self.db, 7, first_fingerprint)

        self.assertTrue(deleted)
        self.assertEqual([int(item["id"]) for item in self.sub2api.accounts], [8])
        self.assertEqual(self.sub2api.accounts[0]["priority"], 40)

    async def test_stale_local_tombstone_is_unbound_and_does_not_consume_capacity(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=0.5, priority=90)
        await self._add_config(8, interval.id, group=1.0, priority=91)
        await self.service.rebalance(self.db)
        self.sub2api.accounts = [
            account for account in self.sub2api.accounts if int(account["id"]) != 7
        ]

        result = await self.service.rebalance(self.db)

        self.assertEqual((result.considered, result.failed), (1, 0))
        self.assertEqual(self.sub2api.accounts[0]["priority"], 40)
        configs = {
            item.sub2api_account_id: item
            for item in (await self.db.execute(select(UpstreamAccountConfig))).scalars().all()
        }
        self.assertIsNone(configs[7].priority_interval_id)
        self.assertEqual(configs[7].priority_sync_status, "unassigned")
        listed = await self.service.list_intervals(self.db)
        self.assertEqual(listed[0].account_count, 1)

    async def test_rebalance_holds_account_lock_through_remote_readback(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=1.0, priority=99)
        self.sub2api.update_started = asyncio.Event()
        self.sub2api.allow_update = asyncio.Event()

        task = asyncio.create_task(self.service.rebalance(self.db))
        await asyncio.wait_for(self.sub2api.update_started.wait(), timeout=1)
        account_lock = await self.accounts._lock_for(7)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(account_lock.acquire(), timeout=0.05)
        self.sub2api.allow_update.set()
        result = await asyncio.wait_for(task, timeout=1)

        self.assertEqual(result.updated, 1)
        await asyncio.wait_for(account_lock.acquire(), timeout=1)
        account_lock.release()

    async def test_rebalance_refreshes_configs_after_waiting_for_account_lock(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=0.5, priority=90)
        await self._add_config(8, interval.id, group=1.0, priority=91)
        account_lock = await self.accounts._lock_for(7)
        await account_lock.acquire()
        task = asyncio.create_task(self.service.rebalance(self.db))
        await asyncio.sleep(0.05)
        async with self.session_factory() as concurrent_db:
            config = (
                await concurrent_db.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 7
                    )
                )
            ).scalar_one()
            config.effective_group_multiplier = 2.0
            await concurrent_db.commit()
        account_lock.release()

        result = await asyncio.wait_for(task, timeout=2)

        self.assertEqual(result.failed, 0)
        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 69, 8: 40})

    async def test_rebalance_drops_config_deleted_while_waiting_for_account_lock(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=0.5, priority=90)
        await self._add_config(8, interval.id, group=1.0, priority=91)
        account_lock = await self.accounts._lock_for(7)
        await account_lock.acquire()
        task = asyncio.create_task(self.service.rebalance(self.db))
        await asyncio.sleep(0.05)
        async with self.session_factory() as concurrent_db:
            config = (
                await concurrent_db.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 7
                    )
                )
            ).scalar_one()
            await concurrent_db.delete(config)
            await concurrent_db.commit()
        account_lock.release()

        result = await asyncio.wait_for(task, timeout=2)

        self.assertEqual((result.considered, result.failed), (1, 0))
        priorities = {int(item["id"]): item["priority"] for item in self.sub2api.accounts}
        self.assertEqual(priorities, {7: 90, 8: 40})

    async def test_bulk_write_rechecks_strict_identity_immediately_before_mutation(self) -> None:
        interval = await self._interval()
        await self._add_config(7, interval.id, group=1.0, priority=90)
        original_detail = self.sub2api.get_account_by_id

        async def replaced_detail(account_id: str | int, **kwargs):
            account = await original_detail(account_id, **kwargs)
            if account is not None:
                account["name"] = "replacement-account"
            return account

        with patch.object(self.sub2api, "get_account_by_id", new=replaced_detail):
            result = await self.service.rebalance(self.db)

        self.assertEqual((result.updated, result.failed), (0, 1))
        self.assertEqual(self.sub2api.priority_update_calls, [])
        config = (await self.db.execute(select(UpstreamAccountConfig))).scalar_one()
        self.assertEqual(config.priority_sync_status, "apply_failed")

    async def test_bulk_write_preflight_is_bounded_and_concurrent(self) -> None:
        interval = await self._interval()
        for account_id in range(1, 13):
            await self._add_config(
                account_id,
                interval.id,
                group=float(account_id),
                priority=90,
            )
        original_detail = self.sub2api.get_account_by_id
        active = 0
        max_active = 0

        async def tracked_detail(account_id: str | int, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.01)
                return await original_detail(account_id, **kwargs)
            finally:
                active -= 1

        with patch.object(self.sub2api, "get_account_by_id", new=tracked_detail):
            result = await self.service.rebalance(self.db)

        self.assertEqual((result.updated, result.failed), (12, 0))
        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, 10)


class PriorityMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_migration_is_idempotent_and_cleans_legacy_overlap_guards(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE upstream_account_configs ("
                        "id INTEGER PRIMARY KEY, sub2api_account_id INTEGER NOT NULL UNIQUE)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO upstream_account_configs (id, sub2api_account_id) "
                        "VALUES (1, 7)"
                    )
                )
                await _migrate_upstream_priority_intervals(connection)
                await connection.execute(
                    text(
                        "CREATE TRIGGER trg_upstream_priority_interval_no_overlap_insert "
                        "BEFORE INSERT ON upstream_priority_intervals "
                        "WHEN EXISTS (SELECT 1 FROM upstream_priority_intervals "
                        "WHERE NEW.start_priority < end_priority AND NEW.end_priority > start_priority) "
                        "BEGIN SELECT RAISE(ABORT, 'overlapping upstream priority interval'); END"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TRIGGER trg_upstream_priority_interval_no_overlap_update "
                        "BEFORE UPDATE OF start_priority, end_priority ON upstream_priority_intervals "
                        "WHEN EXISTS (SELECT 1 FROM upstream_priority_intervals "
                        "WHERE id != OLD.id AND NEW.start_priority < end_priority "
                        "AND NEW.end_priority > start_priority) "
                        "BEGIN SELECT RAISE(ABORT, 'overlapping upstream priority interval'); END"
                    )
                )
                await _migrate_upstream_priority_intervals(connection)
                columns = {
                    str(row[1])
                    for row in (
                        await connection.execute(
                            text("PRAGMA table_info(upstream_account_configs)")
                        )
                    ).fetchall()
                }
                self.assertIn("priority_interval_id", columns)
                self.assertIn("priority_tiebreak_order", columns)
                self.assertIn("priority_tiebreak_multiplier", columns)
                trigger_names = {
                    str(row[1])
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT type, name FROM sqlite_master "
                                "WHERE type = 'trigger' AND name LIKE 'trg_upstream_priority_interval_no_overlap_%'"
                            )
                        )
                    ).fetchall()
                }
                self.assertEqual(trigger_names, set())
                await connection.execute(
                    text(
                        "INSERT INTO upstream_priority_intervals "
                        "(id, name, start_priority, end_priority, step) "
                        "VALUES (1, 'main', 40, 70, 2)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO upstream_priority_intervals "
                        "(id, name, start_priority, end_priority, step) "
                        "VALUES (2, 'overlap', 69, 80, 1)"
                    )
                )
                await connection.execute(
                    text(
                        "UPDATE upstream_account_configs SET priority_interval_id = 1, "
                        "desired_priority = 40, priority_sync_status = 'in_sync', "
                        "priority_sync_error = 'stale' "
                        "WHERE id = 1"
                    )
                )
                await connection.execute(
                    text("DELETE FROM upstream_priority_intervals WHERE id = 1")
                )
                priority_state = (
                    await connection.execute(
                        text(
                            "SELECT priority_interval_id, desired_priority, "
                            "priority_sync_status, priority_sync_error "
                            "FROM upstream_account_configs "
                            "WHERE id = 1"
                        )
                    )
                ).one()
                self.assertEqual(tuple(priority_state), (None, None, "unassigned", None))
        finally:
            await engine.dispose()

    async def test_fresh_schema_delete_clears_complete_priority_state(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("PRAGMA foreign_keys=ON"))
                await connection.run_sync(Base.metadata.create_all)
                await _migrate_upstream_priority_intervals(connection)
                await _migrate_upstream_priority_intervals(connection)

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                interval = UpstreamPriorityInterval(
                    name="main",
                    start_priority=40,
                    end_priority=70,
                    step=2,
                )
                db.add(interval)
                await db.flush()
                db.add(
                    UpstreamAccountConfig(
                        sub2api_account_id=7,
                        priority_interval_id=interval.id,
                        desired_priority=40,
                        priority_sync_status="in_sync",
                        priority_sync_error="stale",
                    )
                )
                await db.commit()

                await db.delete(interval)
                await db.commit()
                db.expire_all()
                config = (
                    await db.execute(select(UpstreamAccountConfig))
                ).scalar_one()

                self.assertEqual(
                    (
                        config.priority_interval_id,
                        config.desired_priority,
                        config.priority_sync_status,
                        config.priority_sync_error,
                    ),
                    (None, None, "unassigned", None),
                )
        finally:
            await engine.dispose()


class PrioritySub2ApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_priority_update_uses_supported_sub2api_payload(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(return_value={})  # type: ignore[method-assign]
        config = SimpleNamespace(accounts_path="/admin/accounts")
        runtime = SimpleNamespace(
            get_sub2api_config=AsyncMock(return_value=config),
        )

        with patch(
            "app.services.sub2api.get_runtime_config_service",
            return_value=runtime,
        ):
            await client.update_account_priorities([8, 7, 8], 40)

        client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "POST",
            "/admin/accounts/bulk-update",
            config=config,
            json={"account_ids": [8, 7], "priority": 40},
        )
        self.assertEqual(client.account_priority({"priority": 0}), 0)
        self.assertIsNone(client.account_priority({"priority": -1}))
        self.assertIsNone(client.account_priority({"priority": True}))


class PriorityRouteTests(unittest.TestCase):
    def test_rebalance_static_route_is_not_captured_as_interval_id(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        fake_db = AsyncMock()
        service = AsyncMock()
        service.rebalance.return_value = PriorityRebalanceOut(
            considered=2,
            updated=1,
            unchanged=1,
            failed=0,
        )

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_upstream_priority_service] = lambda: service
        with TestClient(app) as client:
            response = client.post("/api/upstream-accounts/priority-intervals/rebalance")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated"], 1)
        service.rebalance.assert_awaited_once_with(fake_db)

    def test_equal_multiplier_priority_move_route(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        fake_db = AsyncMock()
        service = AsyncMock()
        service.move_equal_multiplier_priority.return_value = PriorityRebalanceOut(
            considered=2,
            updated=2,
            unchanged=0,
            failed=0,
        )

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_upstream_priority_service] = lambda: service
        fingerprint = "a" * 64
        with TestClient(app) as client:
            response = client.put(
                "/api/upstream-accounts/7/priority-order",
                json={"direction": "up", "expected_identity_fingerprint": fingerprint},
            )

        self.assertEqual(response.status_code, 200, response.text)
        service.move_equal_multiplier_priority.assert_awaited_once()
        args = service.move_equal_multiplier_priority.await_args.args
        self.assertEqual(args[:2], (fake_db, 7))
        self.assertEqual((args[2].direction, args[2].expected_identity_fingerprint), ("up", fingerprint))


if __name__ == "__main__":
    unittest.main()
