from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal, DecimalException
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    UpstreamAccountConfig,
    UpstreamPriorityInterval,
    utcnow,
)
from app.schemas import (
    PriorityIntervalAssignment,
    PriorityIntervalCreate,
    PriorityIntervalOut,
    PriorityIntervalUpdate,
    PriorityRebalanceOut,
    UpstreamAccountOut,
)
from app.services.sub2api import Sub2ApiClient
from app.services.upstream_accounts import (
    UpstreamAccountService,
    UpstreamAccountServiceError,
    _decimal_multiplier,
    get_upstream_account_service,
)


PRIORITY_SYNC_ERROR = "Unable to update and verify the sub2api account priority."
PRIORITY_READ_ERROR = "Unable to read sub2api API key account priorities."
PRIORITY_PREFLIGHT_CONCURRENCY = 10


def composite_multiplier(config: UpstreamAccountConfig) -> Decimal | None:
    group = _decimal_multiplier(config.effective_group_multiplier)
    recharge = _decimal_multiplier(config.effective_recharge_multiplier)
    if group is None or recharge is None:
        return None
    try:
        value = group * recharge
    except DecimalException:
        return None
    return value if value.is_finite() and value > 0 else None


def priority_interval_effective_step(
    start_priority: int,
    end_priority: int,
    configured_step: int,
    account_count: int,
) -> int:
    if account_count <= 1:
        return configured_step
    max_step = (end_priority - start_priority - 1) // (account_count - 1)
    return max(1, min(configured_step, max_step))


def allocate_interval_priorities(
    interval: UpstreamPriorityInterval,
    configs: list[UpstreamAccountConfig],
) -> tuple[dict[int, int], int]:
    ordered = sorted(
        configs,
        key=lambda config: (
            composite_multiplier(config) is None,
            composite_multiplier(config) or Decimal(0),
            config.sub2api_account_id,
        ),
    )
    eligible = [config for config in ordered if composite_multiplier(config) is not None]
    effective_step = priority_interval_effective_step(
        interval.start_priority,
        interval.end_priority,
        interval.step,
        len(eligible),
    )
    final_priority = interval.end_priority - 1
    assignments = {
        config.sub2api_account_id: min(
            interval.start_priority + index * effective_step,
            final_priority,
        )
        for index, config in enumerate(eligible)
    }
    return assignments, effective_step


class UpstreamPriorityService:
    def __init__(
        self,
        sub2api: Sub2ApiClient | None = None,
        accounts: UpstreamAccountService | None = None,
    ) -> None:
        self.accounts = accounts or UpstreamAccountService(sub2api or Sub2ApiClient())
        self.sub2api = self.accounts.sub2api
        self._lock = asyncio.Lock()
        if self.accounts._priorities is None:
            self.accounts._priorities = self

    @staticmethod
    async def _interval_counts(db: AsyncSession) -> dict[int, tuple[int, int]]:
        result = await db.execute(
            select(
                UpstreamAccountConfig.priority_interval_id,
                func.count(UpstreamAccountConfig.id),
            )
            .where(UpstreamAccountConfig.priority_interval_id.is_not(None))
            .group_by(UpstreamAccountConfig.priority_interval_id)
        )
        total_counts = {int(interval_id): int(count) for interval_id, count in result.all()}
        config_result = await db.execute(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.priority_interval_id.is_not(None)
            )
        )
        eligible_counts: dict[int, int] = defaultdict(int)
        for config in config_result.scalars().all():
            if composite_multiplier(config) is not None and config.priority_interval_id is not None:
                eligible_counts[config.priority_interval_id] += 1
        return {
            interval_id: (count, eligible_counts.get(interval_id, 0))
            for interval_id, count in total_counts.items()
        }

    @staticmethod
    def _interval_out(
        interval: UpstreamPriorityInterval,
        *,
        account_count: int,
        eligible_count: int,
    ) -> PriorityIntervalOut:
        return PriorityIntervalOut(
            id=interval.id,
            name=interval.name,
            start_priority=interval.start_priority,
            end_priority=interval.end_priority,
            step=interval.step,
            account_count=account_count,
            effective_step=priority_interval_effective_step(
                interval.start_priority,
                interval.end_priority,
                interval.step,
                eligible_count,
            ),
            created_at=interval.created_at,
            updated_at=interval.updated_at,
        )

    async def list_intervals(self, db: AsyncSession) -> list[PriorityIntervalOut]:
        result = await db.execute(
            select(UpstreamPriorityInterval).order_by(
                UpstreamPriorityInterval.start_priority,
                UpstreamPriorityInterval.id,
            )
        )
        counts = await self._interval_counts(db)
        return [
            self._interval_out(
                interval,
                account_count=counts.get(interval.id, (0, 0))[0],
                eligible_count=counts.get(interval.id, (0, 0))[1],
            )
            for interval in result.scalars().all()
        ]

    @staticmethod
    async def _require_non_overlapping(
        db: AsyncSession,
        *,
        start_priority: int,
        end_priority: int,
        exclude_id: int | None = None,
    ) -> None:
        statement = select(UpstreamPriorityInterval.id).where(
            UpstreamPriorityInterval.start_priority < end_priority,
            UpstreamPriorityInterval.end_priority > start_priority,
        )
        if exclude_id is not None:
            statement = statement.where(UpstreamPriorityInterval.id != exclude_id)
        if (await db.execute(statement.limit(1))).scalar_one_or_none() is not None:
            raise UpstreamAccountServiceError(
                "Priority intervals must not overlap.",
                status_code=409,
            )

    async def create_interval(
        self,
        db: AsyncSession,
        payload: PriorityIntervalCreate,
    ) -> PriorityIntervalOut:
        async with self._lock:
            await self._require_non_overlapping(
                db,
                start_priority=payload.start_priority,
                end_priority=payload.end_priority,
            )
            interval = UpstreamPriorityInterval(
                name=payload.name,
                start_priority=payload.start_priority,
                end_priority=payload.end_priority,
                step=payload.step,
            )
            db.add(interval)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise UpstreamAccountServiceError(
                    "A priority interval with this name or range already exists.",
                    status_code=409,
                ) from None
            await db.refresh(interval)
            return self._interval_out(interval, account_count=0, eligible_count=0)

    async def update_interval(
        self,
        db: AsyncSession,
        interval_id: int,
        payload: PriorityIntervalUpdate,
    ) -> PriorityIntervalOut:
        async with self._lock:
            interval = await db.get(UpstreamPriorityInterval, interval_id)
            if interval is None:
                raise UpstreamAccountServiceError(
                    "The priority interval was not found.",
                    status_code=404,
                )
            await self._require_non_overlapping(
                db,
                start_priority=payload.start_priority,
                end_priority=payload.end_priority,
                exclude_id=interval_id,
            )
            interval.name = payload.name
            interval.start_priority = payload.start_priority
            interval.end_priority = payload.end_priority
            interval.step = payload.step
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise UpstreamAccountServiceError(
                    "A priority interval with this name or range already exists.",
                    status_code=409,
                ) from None
            await self._rebalance_locked(db)
            await db.refresh(interval)
            counts = await self._interval_counts(db)
            account_count, eligible_count = counts.get(interval.id, (0, 0))
            return self._interval_out(
                interval,
                account_count=account_count,
                eligible_count=eligible_count,
            )

    async def delete_interval(self, db: AsyncSession, interval_id: int) -> bool:
        async with self._lock:
            interval = await db.get(UpstreamPriorityInterval, interval_id)
            if interval is None:
                return False
            await db.execute(
                update(UpstreamAccountConfig)
                .where(UpstreamAccountConfig.priority_interval_id == interval_id)
                .values(
                    priority_interval_id=None,
                    desired_priority=None,
                    priority_sync_status="unassigned",
                    priority_sync_error=None,
                )
                .execution_options(synchronize_session=False)
            )
            await db.execute(
                delete(UpstreamPriorityInterval).where(
                    UpstreamPriorityInterval.id == interval_id
                )
            )
            await db.commit()
            db.expire_all()
            return True

    async def assign_interval(
        self,
        db: AsyncSession,
        account_id: int,
        payload: PriorityIntervalAssignment,
    ) -> UpstreamAccountOut:
        async with self._lock:
            account_lock = await self.accounts._lock_for(account_id)
            async with account_lock:
                if payload.confirm_identity_rebind:
                    await self.accounts.bind_legacy_identities(
                        db,
                        {account_id: payload.expected_identity_fingerprint},
                    )
                remote = await self.accounts._remote_account(
                    account_id,
                    payload.expected_identity_fingerprint,
                )
                result = await db.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == account_id
                    )
                )
                config = result.scalar_one_or_none()
                if config is None:
                    config = self.accounts._new_config(remote, account_id)
                    db.add(config)
                else:
                    self.accounts._require_config_binding(remote, config)
                if payload.priority_interval_id is not None:
                    interval = await db.get(
                        UpstreamPriorityInterval,
                        payload.priority_interval_id,
                    )
                    if interval is None:
                        raise UpstreamAccountServiceError(
                            "The priority interval was not found.",
                            status_code=404,
                        )
                config.priority_interval_id = payload.priority_interval_id
                config.desired_priority = None
                config.priority_sync_status = (
                    "pending" if payload.priority_interval_id is not None else "unassigned"
                )
                config.priority_sync_error = None
                await db.commit()
            await self._rebalance_locked(db)

        accounts = await self.accounts.list_accounts(db)
        found = next(
            (item for item in accounts if item.sub2api_account_id == account_id),
            None,
        )
        if found is None:
            raise UpstreamAccountServiceError(
                "The sub2api API key account was not found.",
                status_code=404,
            )
        return found

    async def rebalance(
        self,
        db: AsyncSession,
        *,
        account_ids: set[int] | None = None,
    ) -> PriorityRebalanceOut:
        async with self._lock:
            return await self._rebalance_locked(db, account_ids=account_ids)

    async def _rebalance_locked(
        self,
        db: AsyncSession,
        *,
        account_ids: set[int] | None = None,
    ) -> PriorityRebalanceOut:
        candidate_query = select(UpstreamAccountConfig.sub2api_account_id).where(
            UpstreamAccountConfig.priority_interval_id.is_not(None)
        )
        if account_ids is not None:
            if not account_ids:
                return PriorityRebalanceOut(
                    considered=0,
                    updated=0,
                    unchanged=0,
                    failed=0,
                )
            affected_interval_result = await db.execute(
                select(UpstreamAccountConfig.priority_interval_id)
                .where(
                    UpstreamAccountConfig.sub2api_account_id.in_(account_ids),
                    UpstreamAccountConfig.priority_interval_id.is_not(None),
                )
                .distinct()
            )
            affected_interval_ids = {
                int(interval_id)
                for interval_id in affected_interval_result.scalars().all()
                if interval_id is not None
            }
            if not affected_interval_ids:
                return PriorityRebalanceOut(
                    considered=0,
                    updated=0,
                    unchanged=0,
                    failed=0,
                )
            candidate_query = candidate_query.where(
                UpstreamAccountConfig.priority_interval_id.in_(
                    affected_interval_ids
                )
            )
        candidate_result = await db.execute(
            candidate_query.order_by(UpstreamAccountConfig.sub2api_account_id)
        )
        candidate_ids = [int(value) for value in candidate_result.scalars().all()]
        candidate_id_set = set(candidate_ids)
        if not candidate_ids:
            await db.commit()
            return PriorityRebalanceOut(
                considered=0,
                updated=0,
                unchanged=0,
                failed=0,
            )

        account_locks: list[asyncio.Lock] = []
        try:
            for account_id in candidate_ids:
                account_lock = await self.accounts._lock_for(account_id)
                await account_lock.acquire()
                account_locks.append(account_lock)

            # The initial rows only determine which locks to acquire. Refresh
            # after waiting so a completed discovery/delete cannot leave this
            # rebalance operating on a stale ORM snapshot.
            await db.commit()
            interval_result = await db.execute(
                select(UpstreamPriorityInterval).execution_options(populate_existing=True)
            )
            intervals = {item.id: item for item in interval_result.scalars().all()}
            config_result = await db.execute(
                select(UpstreamAccountConfig).execution_options(populate_existing=True)
            )
            configs = list(config_result.scalars().all())
            assigned = []
            for config in configs:
                if config.sub2api_account_id not in candidate_id_set:
                    continue
                if config.priority_interval_id is None:
                    config.desired_priority = None
                    config.priority_sync_status = "unassigned"
                    config.priority_sync_error = None
                    continue
                if config.priority_interval_id not in intervals:
                    config.priority_interval_id = None
                    config.desired_priority = None
                    config.priority_sync_status = "unassigned"
                    config.priority_sync_error = None
                    continue
                assigned.append(config)
            if not assigned:
                await db.commit()
                return PriorityRebalanceOut(
                    considered=0,
                    updated=0,
                    unchanged=0,
                    failed=0,
                )

            try:
                remote_accounts = await self.accounts._remote_accounts()
            except UpstreamAccountServiceError:
                for config in assigned:
                    config.desired_priority = None
                    config.priority_sync_status = "apply_failed"
                    config.priority_sync_error = PRIORITY_READ_ERROR
                await db.commit()
                return PriorityRebalanceOut(
                    considered=len(assigned),
                    updated=0,
                    unchanged=0,
                    failed=len(assigned),
                )

            remote_by_id = {
                account_id: remote
                for remote in remote_accounts
                if (account_id := self.accounts._numeric_remote_id(remote)) is not None
            }
            live_assigned: list[UpstreamAccountConfig] = []
            by_interval: dict[int, list[UpstreamAccountConfig]] = defaultdict(list)
            for config in assigned:
                remote = remote_by_id.get(config.sub2api_account_id)
                if (
                    remote is None
                    or self.accounts._config_binding_status(remote, config) != "bound"
                ):
                    config.priority_interval_id = None
                    config.desired_priority = None
                    config.priority_sync_status = "unassigned"
                    config.priority_sync_error = None
                    continue
                live_assigned.append(config)
                if composite_multiplier(config) is None:
                    config.desired_priority = None
                    config.priority_sync_status = "multiplier_unavailable"
                    config.priority_sync_error = None
                    continue
                by_interval[config.priority_interval_id or 0].append(config)

            desired_by_id: dict[int, int] = {}
            for interval_id, interval_configs in by_interval.items():
                assignments, _effective_step = allocate_interval_priorities(
                    intervals[interval_id],
                    interval_configs,
                )
                desired_by_id.update(assignments)
                for config in interval_configs:
                    config.desired_priority = assignments[config.sub2api_account_id]
                    config.priority_sync_status = "pending"
                    config.priority_sync_error = None

            if not desired_by_id:
                await db.commit()
                return PriorityRebalanceOut(
                    considered=len(live_assigned),
                    updated=0,
                    unchanged=0,
                    failed=0,
                )
            return await self._apply_desired_priorities(
                db,
                configs=configs,
                assigned_count=len(live_assigned),
                desired_by_id=desired_by_id,
                remote_by_id=remote_by_id,
            )
        finally:
            for account_lock in reversed(account_locks):
                account_lock.release()

    async def _apply_desired_priorities(
        self,
        db: AsyncSession,
        *,
        configs: list[UpstreamAccountConfig],
        assigned_count: int,
        desired_by_id: dict[int, int],
        remote_by_id: dict[int, dict[str, Any]],
    ) -> PriorityRebalanceOut:
        config_by_id = {config.sub2api_account_id: config for config in configs}
        pending_by_priority: dict[int, list[int]] = defaultdict(list)
        unchanged = 0
        failed_ids: set[int] = set()
        attempted_ids: set[int] = set()
        strict_fingerprints = {
            account_id: self.accounts._remote_identity_fingerprint(remote)
            for account_id, remote in remote_by_id.items()
            if account_id in desired_by_id
        }

        for account_id, desired in desired_by_id.items():
            config = config_by_id[account_id]
            remote = remote_by_id.get(account_id)
            if remote is None or self.accounts._config_binding_status(remote, config) != "bound":
                config.priority_sync_status = "apply_failed"
                config.priority_sync_error = PRIORITY_READ_ERROR
                failed_ids.add(account_id)
                continue
            current = self.sub2api.account_priority(remote)
            if current == desired:
                config.priority_sync_status = "in_sync"
                config.priority_sync_error = None
                unchanged += 1
            else:
                pending_by_priority[desired].append(account_id)

        preflight_semaphore = asyncio.Semaphore(PRIORITY_PREFLIGHT_CONCURRENCY)
        group_semaphore = asyncio.Semaphore(PRIORITY_PREFLIGHT_CONCURRENCY)

        async def verify_account(account_id: int) -> bool:
            config = config_by_id[account_id]
            async with preflight_semaphore:
                try:
                    current_remote = await self.sub2api.get_account_by_id(account_id)
                    return bool(
                        current_remote is not None
                        and self.accounts._remote_identity_fingerprint(current_remote)
                        == strict_fingerprints.get(account_id)
                        and self.accounts._config_binding_status(current_remote, config)
                        == "bound"
                    )
                except Exception as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return False

        async def apply_priority_group(
            priority: int,
            account_ids: list[int],
        ) -> tuple[list[int], list[int], bool]:
            async with group_semaphore:
                verification = await asyncio.gather(
                    *(verify_account(account_id) for account_id in account_ids)
                )
                verified_ids = [
                    account_id
                    for account_id, verified in zip(account_ids, verification, strict=True)
                    if verified
                ]
                rejected_ids = [
                    account_id
                    for account_id, verified in zip(account_ids, verification, strict=True)
                    if not verified
                ]
                if not verified_ids:
                    return [], rejected_ids, False
                try:
                    await self.sub2api.update_account_priorities(verified_ids, priority)
                    return verified_ids, rejected_ids, False
                except Exception as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return [], rejected_ids + verified_ids, True

        group_results = await asyncio.gather(
            *(
                apply_priority_group(priority, pending_by_priority[priority])
                for priority in sorted(pending_by_priority)
            )
        )
        for verified_ids, rejected_ids, write_failed in group_results:
            attempted_ids.update(verified_ids)
            failed_ids.update(rejected_ids)
            for account_id in rejected_ids:
                config = config_by_id[account_id]
                config.priority_sync_status = "apply_failed"
                config.priority_sync_error = (
                    PRIORITY_SYNC_ERROR if write_failed else PRIORITY_READ_ERROR
                )

        updated = 0
        if attempted_ids:
            try:
                readback_accounts = await self.accounts._remote_accounts()
            except UpstreamAccountServiceError:
                readback_accounts = []
            readback_by_id = {
                account_id: remote
                for remote in readback_accounts
                if (account_id := self.accounts._numeric_remote_id(remote)) is not None
            }
            for account_id in attempted_ids:
                config = config_by_id[account_id]
                remote = readback_by_id.get(account_id)
                desired = desired_by_id[account_id]
                if (
                    remote is not None
                    and self.accounts._config_binding_status(remote, config) == "bound"
                    and self.sub2api.account_priority(remote) == desired
                ):
                    config.priority_sync_status = "in_sync"
                    config.priority_sync_error = None
                    config.last_priority_applied_at = utcnow()
                    updated += 1
                else:
                    config.priority_sync_status = "apply_failed"
                    config.priority_sync_error = PRIORITY_SYNC_ERROR
                    failed_ids.add(account_id)

        await db.commit()
        return PriorityRebalanceOut(
            considered=assigned_count,
            updated=updated,
            unchanged=unchanged,
            failed=len(failed_ids),
        )


_service: UpstreamPriorityService | None = None


def get_upstream_priority_service() -> UpstreamPriorityService:
    global _service
    if _service is None:
        accounts = get_upstream_account_service()
        existing = accounts._priorities
        if isinstance(existing, UpstreamPriorityService):
            _service = existing
        else:
            _service = UpstreamPriorityService(accounts=accounts)
            accounts._priorities = _service
    return _service
