from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
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
    PriorityTieMoveRequest,
    UpstreamAccountOut,
)
from app.services.sub2api import Sub2ApiClient
from app.services.runtime_config import get_runtime_config_service
from app.services.upstream_accounts import (
    UpstreamAccountService,
    UpstreamAccountServiceError,
    _decimal_multiplier,
    get_upstream_account_service,
)


PRIORITY_SYNC_ERROR = "Unable to update and verify the sub2api account priority."
PRIORITY_READ_ERROR = "Unable to read sub2api API key account priorities."
PRIORITY_PREFLIGHT_CONCURRENCY = 10
TIE_MULTIPLIER_QUANTUM = Decimal("1e-13")
MAX_COMPOSITE_MULTIPLIER = Decimal("1000000")
PRIORITY_STRATEGY_COST_OPTIMIZED = "cost_optimized"
PRIORITY_STRATEGY_FIXED_STEP = "fixed_step"


def _decimal_composite_multiplier(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed > MAX_COMPOSITE_MULTIPLIER:
        return None
    return parsed


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


def _tie_multiplier_key(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        # The database and API expose multipliers as IEEE-754 floats. Key the
        # order from that representable value so persistence and UI grouping agree.
        representable = Decimal(str(float(value)))
        return representable.quantize(TIE_MULTIPLIER_QUANTUM, rounding=ROUND_DOWN)
    except (DecimalException, OverflowError, ValueError):
        return value


def _same_composite_multiplier(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return left is right
    return _tie_multiplier_key(left) == _tie_multiplier_key(right)


def _account_name_key(config: UpstreamAccountConfig) -> tuple[str, int]:
    name = str(config.remote_name or "").strip().casefold()
    return (name or f"\uffff{config.sub2api_account_id}", config.sub2api_account_id)


def _active_tiebreak_order(
    config: UpstreamAccountConfig,
    multiplier: Decimal,
) -> int | None:
    order = config.priority_tiebreak_order
    stored_multiplier = _decimal_composite_multiplier(
        config.priority_tiebreak_multiplier
    )
    if order is None or not _same_composite_multiplier(stored_multiplier, multiplier):
        return None
    return max(0, int(order))


def _priority_sort_key(config: UpstreamAccountConfig) -> tuple[Any, ...]:
    multiplier = composite_multiplier(config)
    if multiplier is None:
        return (True, Decimal(0), True, 0, *_account_name_key(config))
    tiebreak_order = _active_tiebreak_order(config, multiplier)
    return (
        False,
        _tie_multiplier_key(multiplier),
        tiebreak_order is None,
        tiebreak_order or 0,
        *_account_name_key(config),
    )


def priority_interval_effective_step(
    start_priority: int,
    end_priority: int,
    configured_step: int,
    account_count: int,
) -> int:
    if account_count <= 1:
        return configured_step
    max_step = (end_priority - start_priority - 1) // (account_count - 1)
    return max(0, min(configured_step, max_step))


def _geometric_median(multipliers: list[Decimal]) -> Decimal:
    ordered = sorted(multipliers)
    count = len(ordered)
    if count == 0:
        raise ValueError("At least one multiplier is required.")
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] * ordered[middle]).sqrt()


def _cost_efficiency_scores(
    multipliers: list[Decimal],
    *,
    center_multipliers: list[Decimal] | None = None,
) -> list[Decimal]:
    """Return inverse-cost scores centered on the account-weighted median."""
    count = len(multipliers)
    if count == 0:
        return []
    center = _geometric_median(center_multipliers or multipliers)
    return [center / (center + multiplier) for multiplier in multipliers]


def _cost_optimized_priority_group_offsets(
    multipliers: list[Decimal],
    *,
    center_multipliers: list[Decimal],
    available_span: int,
    minimum_step: int,
) -> list[int]:
    """Map inverse-cost efficiency onto the full priority span.

    Sub2API normalizes the priority score from the smallest and largest
    candidate priorities. This mirrors its native upstream-cost curve so
    multiplier ratios, rather than raw differences, determine preference.
    """
    count = len(multipliers)
    if count <= 1:
        return [0] * count

    low = multipliers[0]
    high = multipliers[-1]
    if high > low:
        efficiencies = _cost_efficiency_scores(
            multipliers,
            center_multipliers=center_multipliers,
        )
        highest_efficiency = efficiencies[0]
        lowest_efficiency = efficiencies[-1]
        efficiency_span = highest_efficiency - lowest_efficiency
        targets = [
            int(
                (
                    (highest_efficiency - efficiency)
                    * Decimal(available_span)
                    / efficiency_span
                ).to_integral_value(rounding=ROUND_HALF_UP)
            )
            for efficiency in efficiencies
        ]
        targets[0] = 0
        targets[-1] = available_span
    else:
        # Equal-cost accounts only need the smallest representable separation
        # when sharing is disabled. Do not stretch ties across the whole band.
        targets = (
            [index * minimum_step for index in range(count)]
            if minimum_step > 0
            else [
                (index * available_span) // (count - 1)
                for index in range(count)
            ]
        )

    offsets: list[int] = []
    for index, target in enumerate(targets):
        lower = index * minimum_step
        if offsets:
            lower = max(lower, offsets[-1] + minimum_step)
        upper = available_span - (count - index - 1) * minimum_step
        offsets.append(min(max(target, lower), upper))
    return offsets


def _fixed_step_priority_group_offsets(
    *,
    group_count: int,
    available_span: int,
    effective_step: int,
) -> list[int]:
    # Preserve the original rank-based allocator. If the interval is too
    # small, trailing groups share its exclusive upper bound minus one.
    assignment_step = max(1, effective_step)
    return [
        min(index * assignment_step, available_span)
        for index in range(group_count)
    ]


def allocate_interval_priorities(
    interval: UpstreamPriorityInterval,
    configs: list[UpstreamAccountConfig],
    *,
    share_same_composite_multiplier: bool = False,
) -> tuple[dict[int, int], int]:
    ordered = sorted(
        configs,
        key=_priority_sort_key,
    )
    eligible = [config for config in ordered if composite_multiplier(config) is not None]
    multiplier_groups: list[list[UpstreamAccountConfig]] = []
    if share_same_composite_multiplier:
        for config in eligible:
            multiplier = composite_multiplier(config)
            if (
                multiplier_groups
                and _same_composite_multiplier(
                    composite_multiplier(multiplier_groups[-1][0]),
                    multiplier,
                )
            ):
                multiplier_groups[-1].append(config)
            else:
                multiplier_groups.append([config])
    else:
        multiplier_groups = [[config] for config in eligible]
    strategy = str(
        getattr(interval, "allocation_strategy", None)
        or PRIORITY_STRATEGY_COST_OPTIMIZED
    ).strip().lower()
    configured_step = (
        interval.step
        if strategy == PRIORITY_STRATEGY_FIXED_STEP
        else 1
    )
    effective_step = priority_interval_effective_step(
        interval.start_priority,
        interval.end_priority,
        configured_step,
        len(multiplier_groups),
    )
    available_span = interval.end_priority - interval.start_priority - 1
    group_multipliers = [
        composite_multiplier(group[0])
        for group in multiplier_groups
    ]
    account_multipliers = [
        multiplier
        for config in eligible
        if (multiplier := composite_multiplier(config)) is not None
    ]
    if strategy == PRIORITY_STRATEGY_FIXED_STEP:
        offsets = _fixed_step_priority_group_offsets(
            group_count=len(multiplier_groups),
            available_span=available_span,
            effective_step=effective_step,
        )
    else:
        offsets = _cost_optimized_priority_group_offsets(
            [multiplier for multiplier in group_multipliers if multiplier is not None],
            center_multipliers=account_multipliers,
            available_span=available_span,
            minimum_step=effective_step,
        )
    assignments = {
        config.sub2api_account_id: interval.start_priority + offsets[group_index]
        for group_index, group in enumerate(multiplier_groups)
        for config in group
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
    def _assign_priority_when_disabled(
        config: UpstreamAccountConfig,
        global_enabled: bool,
    ) -> bool:
        override = config.priority_assignment_when_disabled
        return bool(override if override is not None else global_enabled)

    async def _global_assign_disabled(self) -> bool:
        try:
            return bool(
                await get_runtime_config_service().get_priority_assign_disabled_api_key_accounts()
            )
        except Exception:
            return False

    async def _share_same_composite_multiplier(self) -> bool:
        try:
            return bool(
                await get_runtime_config_service().get_priority_share_same_composite_multiplier()
            )
        except Exception:
            return False

    async def _interval_counts(self, db: AsyncSession) -> dict[int, tuple[int, int]]:
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
        eligible_multiplier_groups: dict[int, set[Decimal]] = defaultdict(set)
        global_assign_disabled, share_same_multiplier = await asyncio.gather(
            self._global_assign_disabled(),
            self._share_same_composite_multiplier(),
        )
        for config in config_result.scalars().all():
            enabled_for_priority = bool(
                config.remote_schedulable is not False
                or self._assign_priority_when_disabled(config, global_assign_disabled)
            )
            if (
                enabled_for_priority
                and composite_multiplier(config) is not None
                and config.priority_interval_id is not None
            ):
                if share_same_multiplier:
                    multiplier = composite_multiplier(config)
                    if multiplier is not None:
                        eligible_multiplier_groups[config.priority_interval_id].add(
                            _tie_multiplier_key(multiplier)
                        )
                else:
                    eligible_counts[config.priority_interval_id] += 1
        if share_same_multiplier:
            eligible_counts.update(
                {
                    interval_id: len(multiplier_groups)
                    for interval_id, multiplier_groups in eligible_multiplier_groups.items()
                }
            )
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
            allocation_strategy=(
                interval.allocation_strategy or PRIORITY_STRATEGY_COST_OPTIMIZED
            ),
            rate_pause_enabled=bool(interval.rate_pause_enabled),
            rate_absolute_threshold=float(interval.rate_absolute_threshold or 1.0),
            account_count=account_count,
            effective_step=priority_interval_effective_step(
                interval.start_priority,
                interval.end_priority,
                (
                    interval.step
                    if (
                        interval.allocation_strategy
                        or PRIORITY_STRATEGY_COST_OPTIMIZED
                    ) == PRIORITY_STRATEGY_FIXED_STEP
                    else 1
                ),
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

    async def create_interval(
        self,
        db: AsyncSession,
        payload: PriorityIntervalCreate,
    ) -> PriorityIntervalOut:
        async with self._lock:
            interval = UpstreamPriorityInterval(
                name=payload.name,
                start_priority=payload.start_priority,
                end_priority=payload.end_priority,
                step=payload.step,
                allocation_strategy=payload.allocation_strategy,
                rate_pause_enabled=payload.rate_pause_enabled,
                rate_absolute_threshold=payload.rate_absolute_threshold,
            )
            db.add(interval)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise UpstreamAccountServiceError(
                    "A priority interval with this name already exists.",
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
            interval.name = payload.name
            interval.start_priority = payload.start_priority
            interval.end_priority = payload.end_priority
            interval.step = payload.step
            interval.allocation_strategy = payload.allocation_strategy
            interval.rate_pause_enabled = payload.rate_pause_enabled
            interval.rate_absolute_threshold = payload.rate_absolute_threshold
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise UpstreamAccountServiceError(
                    "A priority interval with this name already exists.",
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
                    priority_tiebreak_order=None,
                    priority_tiebreak_multiplier=None,
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
                config.priority_tiebreak_order = None
                config.priority_tiebreak_multiplier = None
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

    async def move_equal_multiplier_priority(
        self,
        db: AsyncSession,
        account_id: int,
        payload: PriorityTieMoveRequest,
    ) -> PriorityRebalanceOut:
        async with self._lock:
            if await self._share_same_composite_multiplier():
                raise UpstreamAccountServiceError(
                    "Accounts with the same composite multiplier currently share one priority.",
                    status_code=409,
                )
            account_lock = await self.accounts._lock_for(account_id)
            neighbor_id: int
            async with account_lock:
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
                    raise UpstreamAccountServiceError(
                        "The API Key account is not managed locally.",
                        status_code=404,
                    )
                self.accounts._require_config_binding(remote, config)
                multiplier = composite_multiplier(config)
                if config.priority_interval_id is None or multiplier is None:
                    raise UpstreamAccountServiceError(
                        "The API Key account needs a priority interval and composite multiplier before its tie order can be changed.",
                        status_code=409,
                    )
                peer_result = await db.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.priority_interval_id
                        == config.priority_interval_id
                    )
                )
                peers = [
                    item
                    for item in peer_result.scalars().all()
                    if item.desired_priority is not None
                    and _same_composite_multiplier(composite_multiplier(item), multiplier)
                ]
                ordered = sorted(peers, key=_priority_sort_key)
                target_index = next(
                    (
                        index
                        for index, item in enumerate(ordered)
                        if item.sub2api_account_id == account_id
                    ),
                    -1,
                )
                neighbor_index = (
                    target_index + 1 if payload.direction == "up" else target_index - 1
                )
                if (
                    target_index < 0
                    or neighbor_index < 0
                    or neighbor_index >= len(ordered)
                ):
                    raise UpstreamAccountServiceError(
                        "There is no equal-multiplier account in that direction.",
                        status_code=409,
                    )
                ordered[target_index], ordered[neighbor_index] = (
                    ordered[neighbor_index],
                    ordered[target_index],
                )
                stored_multiplier = float(multiplier)
                for index, item in enumerate(ordered):
                    item.priority_tiebreak_order = index
                    item.priority_tiebreak_multiplier = stored_multiplier
                neighbor_id = ordered[target_index].sub2api_account_id
                await db.commit()
            return await self._rebalance_locked(
                db,
                account_ids={account_id, neighbor_id},
            )

    async def rebalance(
        self,
        db: AsyncSession,
        *,
        account_ids: set[int] | None = None,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> PriorityRebalanceOut:
        async with self._lock:
            return await self._rebalance_locked(
                db,
                account_ids=account_ids,
                remote_by_id=remote_by_id,
            )

    async def _rebalance_locked(
        self,
        db: AsyncSession,
        *,
        account_ids: set[int] | None = None,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
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

            # The locked ORM snapshot is complete. Persist local cleanup and
            # release the database transaction before listing remote accounts.
            await db.commit()
            if remote_by_id is None:
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
            global_assign_disabled, share_same_multiplier = await asyncio.gather(
                self._global_assign_disabled(),
                self._share_same_composite_multiplier(),
            )
            live_assigned: list[UpstreamAccountConfig] = []
            by_interval: dict[int, list[UpstreamAccountConfig]] = defaultdict(list)
            for config in assigned:
                remote = remote_by_id.get(config.sub2api_account_id)
                if remote is None and not config.remote_present:
                    # Inventory has already recorded when the account was last
                    # seen. Keep its interval and historical priority so an
                    # incomplete remote list cannot discard user configuration.
                    config.priority_sync_status = "remote_missing"
                    config.priority_sync_error = None
                    continue
                if remote is None or self.accounts._config_binding_status(remote, config) != "bound":
                    config.priority_interval_id = None
                    config.desired_priority = None
                    config.priority_sync_status = "unassigned"
                    config.priority_sync_error = None
                    continue
                live_assigned.append(config)
                self.accounts.apply_remote_snapshot(config, remote)
                if (
                    self.sub2api.account_schedulable(remote) is False
                    and not self._assign_priority_when_disabled(
                        config,
                        global_assign_disabled,
                    )
                ):
                    config.desired_priority = None
                    config.priority_sync_status = "disabled_excluded"
                    config.priority_sync_error = None
                    continue
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
                    share_same_composite_multiplier=share_same_multiplier,
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
            # Store the desired plan before its remote preflight and mutation.
            # Account locks remain held, while SQLite is free for other jobs.
            await db.commit()
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
            readback_succeeded = True
            try:
                readback_accounts = await self.accounts._remote_accounts()
            except UpstreamAccountServiceError:
                readback_accounts = []
                readback_succeeded = False
            readback_by_id = {
                account_id: remote
                for remote in readback_accounts
                if (account_id := self.accounts._numeric_remote_id(remote)) is not None
            }
            if readback_succeeded:
                # Callers may share this snapshot with the final overview. Keep
                # it authoritative after priority writes instead of listing all
                # accounts yet again outside the priority workflow.
                remote_by_id.clear()
                remote_by_id.update(readback_by_id)
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
