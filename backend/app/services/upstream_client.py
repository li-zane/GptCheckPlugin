from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import math
import re
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal, Sequence
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


UpstreamType = Literal["auto", "newapi", "sub2api"]
ResolverResult = Iterable[str | ipaddress.IPv4Address | ipaddress.IPv6Address]
Resolver = Callable[[str], ResolverResult | Awaitable[ResolverResult]]

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PINNED_ADDRESSES = 8
MAX_UPSTREAM_TOKEN_LENGTH = 8192
DEFAULT_TIMEOUT_SECONDS = 3.5
# Some upstream deployments aggregate usage on demand. Keep this isolated
# from balance/group/status probes while preserving longer caller timeouts.
UPSTREAM_USAGE_TIMEOUT_SECONDS = 60.0
DEFAULT_NEWAPI_QUOTA_PER_UNIT = 500_000.0
DEFAULT_TODAY_TIME_ZONE = "Asia/Shanghai"
# Bound list pagination, detail discovery, and usage batches independently so
# large upstream accounts remain supported without unbounded request fan-out.
MAX_AUTOMATIC_KEY_REVEALS = 200
KEY_REVEAL_CONCURRENCY = 20
SUB2API_API_KEY_USAGE_BATCH_SIZE = 100
SUB2API_API_KEY_PAGE_SIZE = 200
SUB2API_API_KEY_PAGE_CONCURRENCY = 5
MAX_SUB2API_API_KEY_PAGES = 25
MAX_CHANNEL_MONITORS = 100
CHANNEL_MONITOR_DETAIL_CONCURRENCY = 10
MAX_CHANNEL_MONITOR_EXTRA_MODELS = 20
MAX_CHANNEL_MONITOR_TIMELINE_POINTS = 60
NEWAPI_BALANCE_ENDPOINT = "/api/user/self"
NEWAPI_TODAY_USAGE_ENDPOINT = "/api/log/self/stat"
NEWAPI_YESTERDAY_USAGE_RESPONSE_KEY = "newapi:yesterday-usage"
NEWAPI_UPTIME_STATUS_ENDPOINT = "/api/uptime/status"
SUB2API_BALANCE_ENDPOINT = "/api/v1/auth/me"
SUB2API_TODAY_USAGE_ENDPOINT = "/api/v1/usage/dashboard/stats"
SUB2API_USAGE_STATS_ENDPOINT = "/api/v1/usage/stats"
SUB2API_API_KEY_USAGE_ENDPOINT = "/api/v1/usage/dashboard/api-keys-usage"
SUB2API_CHANNEL_MONITORS_ENDPOINT = "/api/v1/channel-monitors"
SUB2API_REFRESH_ENDPOINT = "/api/v1/auth/refresh"
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DNS_OVER_HTTPS_URL = "https://cloudflare-dns.com/dns-query"
MAX_DOH_RESPONSE_BYTES = 128 * 1024
DNS_CACHE_TTL_SECONDS = 15 * 60
MAX_DNS_CACHE_ENTRIES = 256
_DNS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_DNS_CACHE_LOCK = threading.Lock()

# These are deliberately fixed.  Discovery never interpolates a user-provided
# path or query string into an upstream request.
NEWAPI_ENDPOINTS: tuple[str, ...] = (
    "/api/user/self/groups",
    "/api/pricing",
    "/api/v1/groups/available",
    "/api/user/self",
    NEWAPI_TODAY_USAGE_ENDPOINT,
    "/api/token/?p=1&page_size=200",
    "/api/token/search?p=1&size=200",
    "/api/v1/keys?page=1&page_size=200",
    "/api/status",
    NEWAPI_UPTIME_STATUS_ENDPOINT,
    "/api/v1/payment/config",
    "/api/v1/payment/checkout-info",
)

SUB2API_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/groups/available",
    "/api/v1/groups/rates",
    "/api/v1/keys?page=1&page_size=200",
    "/api/v1/api-keys?page=1&page_size=200",
    "/api/v1/payment/checkout-info",
    "/api/v1/payment/config",
    SUB2API_BALANCE_ENDPOINT,
    SUB2API_TODAY_USAGE_ENDPOINT,
    SUB2API_USAGE_STATS_ENDPOINT,
    SUB2API_CHANNEL_MONITORS_ENDPOINT,
)

NEWAPI_PRIMARY_ENDPOINTS: tuple[str, ...] = (
    "/api/user/self/groups",
    "/api/pricing",
    "/api/user/self",
    NEWAPI_TODAY_USAGE_ENDPOINT,
    "/api/token/?p=1&page_size=200",
    "/api/v1/payment/checkout-info",
    "/api/status",
    NEWAPI_UPTIME_STATUS_ENDPOINT,
)
SUB2API_PRIMARY_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/groups/available",
    "/api/v1/groups/rates",
    "/api/v1/keys?page=1&page_size=200",
    "/api/v1/payment/checkout-info",
    SUB2API_BALANCE_ENDPOINT,
    SUB2API_TODAY_USAGE_ENDPOINT,
    SUB2API_USAGE_STATS_ENDPOINT,
    SUB2API_CHANNEL_MONITORS_ENDPOINT,
)
NEWAPI_MANAGEMENT_EVIDENCE_ENDPOINTS = frozenset(
    {
        "/api/user/self/groups",
        NEWAPI_BALANCE_ENDPOINT,
        NEWAPI_TODAY_USAGE_ENDPOINT,
        "/api/token/?p=1&page_size=200",
        "/api/token/search?p=1&size=200",
        "/api/v1/keys?page=1&page_size=200",
    }
)
SUB2API_MANAGEMENT_EVIDENCE_ENDPOINTS = frozenset(
    endpoint
    for endpoint in SUB2API_ENDPOINTS
    if endpoint != SUB2API_CHANNEL_MONITORS_ENDPOINT
)


@dataclass(frozen=True, slots=True)
class GroupOption:
    """A selectable upstream billing group."""

    id: str
    name: str
    multiplier: float
    description: str | None = None
    source: str | None = None

    @property
    def group_id(self) -> str:
        """Compatibility name for callers that prefer ``group_id``."""

        return self.id

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AccountGroupMatch:
    """An API key's actual upstream group, even when its rate is unavailable."""

    id: str
    name: str
    multiplier: float | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class AccountUpstreamState:
    """Authoritative API-key and group state discovered for one account."""

    key_record_id: int | None = None
    key_status: str | None = None
    group_status: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    usage_amount: float | None = None
    usage_unit: str | None = None


@dataclass(slots=True)
class DiscoveryResult:
    """Safe, presentation-ready result of upstream multiplier discovery."""

    upstream_type: str
    source: str
    status: str
    groups: list[GroupOption] = field(default_factory=list)
    matched_group: GroupOption | None = None
    account_group_matches: dict[int, AccountGroupMatch] = field(default_factory=dict)
    matched_account_state: AccountUpstreamState | None = None
    account_upstream_states: dict[int, AccountUpstreamState] = field(default_factory=dict)
    discovered_group_multiplier: float | None = None
    discovered_group_multiplier_source: str | None = None
    discovered_recharge_multiplier: float | None = None
    discovered_recharge_multiplier_source: str | None = None
    recharge_discovery_status: str = "unknown"
    balance_remaining: float | None = None
    balance_total: float | None = None
    balance_used: float | None = None
    balance_unit: str | None = None
    balance_status: str = "unknown"
    balance_message: str = ""
    today_balance_used: float | None = None
    today_balance_unit: str | None = None
    today_balance_status: str = "unknown"
    today_balance_error: str | None = None
    yesterday_balance_used: float | None = None
    yesterday_balance_unit: str | None = None
    yesterday_balance_status: str = "unknown"
    yesterday_balance_error: str | None = None
    channel_monitors: list[dict[str, Any]] = field(default_factory=list)
    channel_monitors_total: int = 0
    channel_monitors_status: str = "unknown"
    channel_monitors_message: str = ""
    sub2api_auth_rejected: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _FetchResult:
    ok: bool
    status_code: int | None = None
    payload: Any = None
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class _BalanceDiscovery:
    remaining: float | None = None
    total: float | None = None
    used: float | None = None
    unit: str | None = None
    status: str = "unknown"
    message: str = ""


@dataclass(frozen=True, slots=True)
class _AvailableGroupRefs:
    ids: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()
    authoritative: bool = False


@dataclass(frozen=True, slots=True)
class Sub2ApiTokenPair:
    access_token: str
    refresh_token: str
    expires_in: int | None = None


class _PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """Connect to one validated IP while retaining the logical HTTP origin.

    The inner HTTP transport derives its TCP destination from ``request.url``.
    Replacing only that host with a numeric address prevents a second DNS
    lookup after validation.  The original Host header remains untouched and
    httpcore's supported ``sni_hostname`` extension keeps TLS certificate
    validation bound to the configured hostname rather than the pinned IP.
    """

    def __init__(
        self,
        *,
        hostname: str,
        address: str,
        inner: httpx.AsyncBaseTransport | None = None,
        close_inner: bool = True,
    ) -> None:
        self.hostname = hostname.rstrip(".").casefold()
        self.address = str(ipaddress.ip_address(address))
        self.inner = inner or httpx.AsyncHTTPTransport(trust_env=False)
        self.close_inner = close_inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_hostname = request.url.host.rstrip(".").casefold()
        if request_hostname != self.hostname:
            raise httpx.ConnectError("Pinned transport rejected an unexpected host.", request=request)

        extensions = dict(request.extensions)
        if request.url.scheme == "https":
            extensions["sni_hostname"] = self.hostname
        pinned_request = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=self.address),
            headers=request.headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self.inner.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        if self.close_inner:
            await self.inner.aclose()


class UpstreamClient:
    """Read-only NewAPI/Sub2API discovery adapter.

    The resolver is injectable both for deterministic tests and for callers
    that already have a hardened DNS policy.  Every resolved address must be a
    globally routable IP address.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        timeout = _positive_number(timeout_seconds)
        if timeout is None:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        # The security contract caps responses at one MiB even if a caller
        # accidentally asks for a larger limit.
        self.timeout_seconds = timeout
        self.max_response_bytes = min(max_response_bytes, MAX_RESPONSE_BYTES)
        self.transport = transport
        self.cache_dns = resolver is None and transport is None
        self.resolver = resolver or _default_resolver

    async def discover(
        self,
        base_url: str,
        *,
        upstream_type: UpstreamType | str = "auto",
        api_key: str | None = None,
        access_token: str | None = None,
        new_api_user: str | int | None = None,
        selected_group_id: str | int | None = None,
        selected_group_name: str | None = None,
        account_api_keys: Mapping[int | str, str] | None = None,
        account_api_key_record_ids: Mapping[int | str, int | str] | None = None,
        optimized_endpoint_fallbacks: bool = False,
        include_channel_monitors: bool = True,
        include_channel_monitor_details: bool = False,
        monitor_only: bool = False,
        today_timezone: str = DEFAULT_TODAY_TIME_ZONE,
    ) -> DiscoveryResult:
        raw_account_api_keys = account_api_keys if isinstance(account_api_keys, Mapping) else {}
        secrets = (api_key, access_token, *raw_account_api_keys.values())
        normalized_account_api_keys = _normalize_account_api_keys(raw_account_api_keys)
        normalized_account_api_key_record_ids = _normalize_account_api_key_record_ids(
            account_api_key_record_ids
            if isinstance(account_api_key_record_ids, Mapping)
            else {}
        )
        normalized_api_key = _clean_secret(api_key)
        target_api_keys = set(normalized_account_api_keys.values())
        if normalized_api_key is not None:
            target_api_keys.add(normalized_api_key)

        def safe(result: DiscoveryResult) -> DiscoveryResult:
            return _scrub_discovery_result(result, secrets)

        requested_type = _clean_upstream_type(upstream_type)
        if requested_type is None:
            return safe(_error_result("auto", "configured", "Unsupported upstream API type."))
        try:
            normalized_url, hostname = _normalize_base_url(base_url)
            if urlparse(normalized_url).scheme != "https":
                return safe(
                    DiscoveryResult(
                        upstream_type=requested_type,
                        source="configured",
                        status="insecure_url",
                        message="Upstream discovery requires HTTPS.",
                    )
                )
            # Validate every caller-supplied header value before opening a
            # transport. Endpoint-specific routing below decides which secret
            # is actually sent.
            _build_headers(
                access_token=access_token,
                api_key=api_key,
                new_api_user=new_api_user,
            )
            pinned_addresses = await self._resolve_public_addresses(hostname)
        except Exception:
            # Never include the supplied URL, resolver exception, or secret in
            # a user-facing error.
            return safe(_error_result(requested_type, "configured", "Upstream URL or credentials are invalid."))

        monitor_endpoints = (
            (NEWAPI_UPTIME_STATUS_ENDPOINT,)
            if requested_type == "newapi"
            else (SUB2API_CHANNEL_MONITORS_ENDPOINT,)
            if requested_type == "sub2api"
            else (NEWAPI_UPTIME_STATUS_ENDPOINT, SUB2API_CHANNEL_MONITORS_ENDPOINT)
        )
        endpoints = (
            monitor_endpoints
            if monitor_only
            else _ordered_union(NEWAPI_ENDPOINTS, SUB2API_ENDPOINTS)
            if requested_type == "auto"
            else (
                NEWAPI_PRIMARY_ENDPOINTS
                if optimized_endpoint_fallbacks and requested_type == "newapi"
                else SUB2API_PRIMARY_ENDPOINTS
                if optimized_endpoint_fallbacks and requested_type == "sub2api"
                else NEWAPI_ENDPOINTS
                if requested_type == "newapi"
                else SUB2API_ENDPOINTS
            )
        )
        if not monitor_only and not include_channel_monitors:
            endpoints = tuple(
                endpoint
                for endpoint in endpoints
                if endpoint not in {
                    NEWAPI_UPTIME_STATUS_ENDPOINT,
                    SUB2API_CHANNEL_MONITORS_ENDPOINT,
                }
            )
        newapi_today_usage_params = _newapi_today_usage_params(today_timezone)
        newapi_yesterday_usage_params = _newapi_yesterday_usage_params(today_timezone)

        timeout = httpx.Timeout(self.timeout_seconds)
        fetched: list[_FetchResult] = []
        newapi_yesterday_usage_result: _FetchResult | None = None
        revealed_api_key_records: dict[str, dict[str, Any]] = {}
        sub2api_api_key_page_payloads: dict[str, Any] = {}
        sub2api_api_key_usage_by_key: dict[str, float] = {}
        cached_api_key_records_by_account: dict[int, dict[str, Any]] = {}
        sub2api_api_key_usage_by_account: dict[int, float] = {}
        sub2api_channel_monitor_details: dict[int, _FetchResult] = {}
        try:
            for address in pinned_addresses:
                pinned_transport = _PinnedAsyncTransport(
                    hostname=hostname,
                    address=address,
                    inner=self.transport,
                    close_inner=self.transport is None,
                )
                async with httpx.AsyncClient(
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "gpt-check-upstream-discovery/1.0",
                    },
                    timeout=timeout,
                    transport=pinned_transport,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    async def fetch_endpoint(endpoint: str) -> _FetchResult:
                        endpoint_headers = _headers_for_endpoint(
                            endpoint,
                            requested_type=requested_type,
                            access_token=access_token,
                            api_key=api_key,
                            new_api_user=new_api_user,
                        )
                        if endpoint_headers is None:
                            return _FetchResult(ok=False, error_kind="credentials_missing")
                        endpoint_params = (
                            newapi_today_usage_params
                            if endpoint == NEWAPI_TODAY_USAGE_ENDPOINT
                            else _sub2api_yesterday_usage_params(today_timezone)
                            if endpoint == SUB2API_USAGE_STATS_ENDPOINT
                            else None
                        )
                        return await self._request_json(
                            client,
                            normalized_url,
                            endpoint,
                            headers=endpoint_headers,
                            params=endpoint_params,
                            timeout_seconds=(
                                max(
                                    self.timeout_seconds,
                                    UPSTREAM_USAGE_TIMEOUT_SECONDS,
                                )
                                if endpoint
                                in {
                                    NEWAPI_TODAY_USAGE_ENDPOINT,
                                    SUB2API_TODAY_USAGE_ENDPOINT,
                                    SUB2API_USAGE_STATS_ENDPOINT,
                                }
                                else None
                            ),
                        )

                    fetched = await asyncio.gather(
                        *(fetch_endpoint(endpoint) for endpoint in endpoints)
                    )
                    if (
                        optimized_endpoint_fallbacks
                        and not monitor_only
                        and requested_type in {"newapi", "sub2api"}
                    ):
                        primary_responses = dict(zip(endpoints, fetched))
                        compatibility_endpoints = _missing_compatibility_endpoints(
                            requested_type,
                            primary_responses,
                        )
                        if not include_channel_monitors:
                            compatibility_endpoints = tuple(
                                endpoint
                                for endpoint in compatibility_endpoints
                                if endpoint not in {
                                    NEWAPI_UPTIME_STATUS_ENDPOINT,
                                    SUB2API_CHANNEL_MONITORS_ENDPOINT,
                                }
                            )
                        if compatibility_endpoints:
                            compatibility_results = await asyncio.gather(
                                *(fetch_endpoint(endpoint) for endpoint in compatibility_endpoints)
                            )
                            endpoints = (*endpoints, *compatibility_endpoints)
                            fetched = [*fetched, *compatibility_results]
                    candidate_responses = dict(zip(endpoints, fetched))
                    candidate_type = (
                        _detect_upstream_type(candidate_responses)
                        if requested_type == "auto"
                        else requested_type
                    )
                    async def fetch_monitor_details() -> dict[int, _FetchResult]:
                        if candidate_type != "sub2api" or not include_channel_monitor_details:
                            return {}
                        try:
                            return await self._fetch_sub2api_channel_monitor_details(
                                client,
                                normalized_url,
                                list_result=candidate_responses.get(
                                    SUB2API_CHANNEL_MONITORS_ENDPOINT
                                ),
                                access_token=access_token,
                            )
                        except Exception:
                            # List summaries remain useful when an older
                            # Sub2API does not expose per-monitor status.
                            return {}

                    async def fetch_newapi_yesterday_usage() -> _FetchResult | None:
                        if candidate_type != "newapi" or monitor_only:
                            return None
                        yesterday_headers = _headers_for_endpoint(
                            NEWAPI_TODAY_USAGE_ENDPOINT,
                            requested_type=requested_type,
                            access_token=access_token,
                            api_key=api_key,
                            new_api_user=new_api_user,
                        )
                        if yesterday_headers is None:
                            return _FetchResult(
                                ok=False,
                                error_kind="credentials_missing",
                            )
                        return await self._request_json(
                            client,
                            normalized_url,
                            NEWAPI_TODAY_USAGE_ENDPOINT,
                            headers=yesterday_headers,
                            params=newapi_yesterday_usage_params,
                            timeout_seconds=max(
                                self.timeout_seconds,
                                UPSTREAM_USAGE_TIMEOUT_SECONDS,
                            ),
                        )

                    async def fetch_api_key_context() -> tuple[
                        dict[str, Any],
                        dict[str, dict[str, Any]],
                        dict[str, float],
                        dict[int, dict[str, Any]],
                        dict[int, float],
                    ]:
                        if (
                            monitor_only
                            or candidate_type not in {"newapi", "sub2api"}
                            or (
                                not target_api_keys
                                and not normalized_account_api_key_record_ids
                            )
                        ):
                            return {}, {}, {}, {}, {}
                        candidate_endpoints = (
                            NEWAPI_ENDPOINTS if candidate_type == "newapi" else SUB2API_ENDPOINTS
                        )
                        candidate_payloads = {
                            endpoint: result.payload
                            for endpoint in candidate_endpoints
                            if (result := candidate_responses.get(endpoint)) is not None
                            and result.ok
                            and _payload_succeeded(result.payload)
                        }

                        cached_records_by_account = (
                            await self._fetch_cached_api_key_records(
                                client,
                                normalized_url,
                                upstream_type=candidate_type,
                                payloads=candidate_payloads,
                                record_ids_by_account=normalized_account_api_key_record_ids,
                                api_keys_by_account=normalized_account_api_keys,
                                access_token=access_token,
                                new_api_user=new_api_user,
                            )
                        )
                        fallback_account_api_keys = {
                            account_id: key
                            for account_id, key in normalized_account_api_keys.items()
                            if account_id not in cached_records_by_account
                        }
                        key_target_api_keys = set(fallback_account_api_keys.values())
                        if normalized_api_key is not None:
                            key_target_api_keys.add(normalized_api_key)

                        initially_matched = _matched_target_api_key_records(
                            candidate_type,
                            candidate_payloads,
                            key_target_api_keys,
                            {},
                        )
                        initially_matched_keys = set(initially_matched)

                        async def fetch_pages() -> dict[str, Any]:
                            if candidate_type != "sub2api":
                                return {}
                            try:
                                return await self._fetch_sub2api_api_key_pages(
                                    client,
                                    normalized_url,
                                    payloads=candidate_payloads,
                                    target_keys=key_target_api_keys,
                                    access_token=access_token,
                                )
                            except Exception:
                                return {}

                        async def fetch_usage(
                            keys: set[str],
                            *,
                            revealed_records: Mapping[str, dict[str, Any]] | None = None,
                        ) -> dict[str, float]:
                            if candidate_type != "sub2api" or not keys:
                                return {}
                            try:
                                return await self._fetch_sub2api_api_key_usage(
                                    client,
                                    normalized_url,
                                    payloads=candidate_payloads,
                                    revealed_records=revealed_records or {},
                                    target_keys=keys,
                                    access_token=access_token,
                                )
                            except Exception:
                                return {}

                        # Page-one IDs are already authoritative enough for usage.
                        # Do not hold those requests behind unrelated later pages.
                        async def fetch_cached_usage() -> dict[int, float]:
                            if candidate_type != "sub2api" or not cached_records_by_account:
                                return {}
                            record_ids_by_account = {
                                account_id: record_id
                                for account_id, record in cached_records_by_account.items()
                                if (
                                    record_id := _api_key_record_id(record)
                                ) is not None
                            }
                            try:
                                usage_by_record_id = await self._fetch_sub2api_usage_by_record_ids(
                                    client,
                                    normalized_url,
                                    record_ids=set(record_ids_by_account.values()),
                                    access_token=access_token,
                                )
                            except Exception:
                                return {}
                            return {
                                account_id: usage_by_record_id[record_id]
                                for account_id, record_id in record_ids_by_account.items()
                                if record_id in usage_by_record_id
                            }

                        page_payloads, usage_by_key, cached_usage_by_account = await asyncio.gather(
                            fetch_pages(),
                            fetch_usage(initially_matched_keys),
                            fetch_cached_usage(),
                        )
                        candidate_payloads.update(page_payloads)

                        directly_matched = _matched_target_api_key_records(
                            candidate_type,
                            candidate_payloads,
                            key_target_api_keys,
                            {},
                        )
                        directly_matched_keys = set(directly_matched)
                        later_direct_keys = directly_matched_keys - initially_matched_keys

                        async def reveal_pending() -> dict[str, dict[str, Any]]:
                            try:
                                return await self._reveal_api_key_records(
                                    client,
                                    normalized_url,
                                    upstream_type=candidate_type,
                                    payloads=candidate_payloads,
                                    target_keys=key_target_api_keys,
                                    access_token=access_token,
                                    new_api_user=new_api_user,
                                )
                            except Exception:
                                # Group and balance discovery remain useful when a
                                # provider does not support automatic key reveal.
                                return {}

                        revealed_records, later_direct_usage = await asyncio.gather(
                            reveal_pending(),
                            fetch_usage(later_direct_keys),
                        )
                        usage_by_key.update(later_direct_usage)

                        unresolved_usage_keys = set(revealed_records) - directly_matched_keys
                        usage_by_key.update(
                            await fetch_usage(
                                unresolved_usage_keys,
                                revealed_records=revealed_records,
                            )
                        )
                        return (
                            page_payloads,
                            revealed_records,
                            usage_by_key,
                            cached_records_by_account,
                            cached_usage_by_account,
                        )

                    (
                        sub2api_channel_monitor_details,
                        newapi_yesterday_usage_result,
                        api_key_context,
                    ) = await asyncio.gather(
                        fetch_monitor_details(),
                        fetch_newapi_yesterday_usage(),
                        fetch_api_key_context(),
                    )
                    (
                        sub2api_api_key_page_payloads,
                        revealed_api_key_records,
                        sub2api_api_key_usage_by_key,
                        cached_api_key_records_by_account,
                        sub2api_api_key_usage_by_account,
                    ) = api_key_context
                # A status code proves that this pinned address completed an
                # HTTP exchange. Do not route any request to another address
                # after that point; fallback is only for total connect/timeout
                # failure across the complete fixed endpoint set.
                if any(result.status_code is not None for result in fetched):
                    break
            if (
                self.cache_dns
                and fetched
                and not any(result.status_code is not None for result in fetched)
            ):
                _invalidate_dns_cache(hostname)
        except Exception:
            return safe(_error_result(requested_type, "configured", "Could not reach upstream service."))
        finally:
            if self.transport is not None:
                await self.transport.aclose()

        responses = dict(zip(endpoints, fetched))
        if newapi_yesterday_usage_result is not None:
            responses[NEWAPI_YESTERDAY_USAGE_RESPONSE_KEY] = newapi_yesterday_usage_result
        if requested_type == "auto":
            detected_type = _detect_upstream_type(responses)
            if detected_type is None:
                newapi_monitor_result = responses.get(NEWAPI_UPTIME_STATUS_ENDPOINT)
                sub2api_monitor_result = responses.get(SUB2API_CHANNEL_MONITORS_ENDPOINT)
                if (
                    monitor_only
                    and newapi_monitor_result is not None
                    and newapi_monitor_result.status_code in {404, 405}
                    and sub2api_monitor_result is not None
                    and sub2api_monitor_result.status_code in {401, 403}
                ):
                    detected_type = "sub2api"
                else:
                    return safe(
                        _discovery_failure(requested_type, "auto", endpoints, responses)
                    )
            active_type = detected_type
            source = "auto"
        else:
            active_type = requested_type
            source = "configured"

        active_endpoints = NEWAPI_ENDPOINTS if active_type == "newapi" else SUB2API_ENDPOINTS
        sub2api_auth_rejected = (
            active_type == "sub2api"
            and (auth_result := responses.get(SUB2API_BALANCE_ENDPOINT)) is not None
            and auth_result.status_code == 401
        )
        usable = {
            endpoint: result.payload
            for endpoint in active_endpoints
            if (result := responses.get(endpoint)) is not None
            and result.ok
            and _payload_succeeded(result.payload)
        }
        if active_type == "sub2api":
            usable.update(sub2api_api_key_page_payloads)
        if monitor_only:
            try:
                (
                    channel_monitors,
                    channel_monitors_total,
                    channel_monitors_status,
                    channel_monitors_message,
                ) = _discover_channel_monitors(
                    active_type,
                    responses,
                    access_token=access_token,
                    secrets=secrets,
                    detail_results=sub2api_channel_monitor_details,
                )
            except Exception:
                return safe(
                    _error_result(
                        active_type,
                        source,
                        "Could not parse a valid upstream monitor response.",
                    )
                )
            credentials_rejected = channel_monitors_status == "credentials_rejected"
            monitor_error = channel_monitors_status == "error"
            return safe(
                DiscoveryResult(
                    upstream_type=active_type,
                    source=source,
                    status="error" if credentials_rejected or monitor_error else "ok",
                    channel_monitors=channel_monitors,
                    channel_monitors_total=channel_monitors_total,
                    channel_monitors_status=channel_monitors_status,
                    channel_monitors_message=channel_monitors_message,
                    sub2api_auth_rejected=(
                        active_type == "sub2api" and credentials_rejected
                    ),
                    message=channel_monitors_message,
                )
            )

        management_evidence_endpoints = (
            NEWAPI_MANAGEMENT_EVIDENCE_ENDPOINTS
            if active_type == "newapi"
            else SUB2API_MANAGEMENT_EVIDENCE_ENDPOINTS
        )
        management_usable = {
            endpoint: payload
            for endpoint, payload in usable.items()
            if endpoint in management_evidence_endpoints
        }
        if not management_usable:
            return safe(_discovery_failure(active_type, source, active_endpoints, responses))

        try:
            groups = _discover_groups(active_type, usable)
            matched_record = (
                revealed_api_key_records.get(normalized_api_key or "")
                or _find_unique_api_key_record(active_type, usable, normalized_api_key)
            )
            available_group_refs = _available_group_refs(active_type, usable)
            matched_group = _select_group(
                groups,
                matched_record,
                selected_group_id=selected_group_id,
                selected_group_name=selected_group_name,
                available_groups=available_group_refs,
            )
            account_group_matches = _match_account_groups(
                active_type,
                usable,
                groups,
                {
                    account_id: key
                    for account_id, key in normalized_account_api_keys.items()
                    if account_id not in cached_api_key_records_by_account
                },
                revealed_api_key_records,
                available_group_refs,
            )
            matched_account_state = _account_upstream_state_from_record(
                active_type,
                matched_record,
                available_group_refs,
            )
            account_upstream_states = _match_account_upstream_states(
                active_type,
                usable,
                {
                    account_id: key
                    for account_id, key in normalized_account_api_keys.items()
                    if account_id not in cached_api_key_records_by_account
                },
                revealed_api_key_records,
                available_group_refs,
            )
            for account_id, record in cached_api_key_records_by_account.items():
                cached_group = _account_group_match_from_record(
                    groups,
                    record,
                    authoritative_groups=available_group_refs.authoritative,
                )
                if cached_group is not None:
                    account_group_matches[account_id] = cached_group
                cached_state = _account_upstream_state_from_record(
                    active_type,
                    record,
                    available_group_refs,
                )
                if cached_state is not None:
                    account_upstream_states[account_id] = cached_state
            if active_type == "sub2api":
                matched_account_state = _state_with_usage(
                    matched_account_state,
                    sub2api_api_key_usage_by_key.get(normalized_api_key or ""),
                )
                for account_id, account_key in normalized_account_api_keys.items():
                    if account_id in cached_api_key_records_by_account:
                        usage_amount = sub2api_api_key_usage_by_account.get(account_id)
                    else:
                        usage_amount = sub2api_api_key_usage_by_key.get(account_key)
                    if usage_amount is None:
                        continue
                    account_upstream_states[account_id] = _state_with_usage(
                        account_upstream_states.get(account_id),
                        usage_amount,
                    )
            for account_group in account_group_matches.values():
                if (
                    account_group.multiplier is not None
                    and _lookup_group(groups, account_group.id, account_group.name) is None
                ):
                    groups.append(
                        GroupOption(
                            id=account_group.id,
                            name=account_group.name,
                            multiplier=account_group.multiplier,
                            source=account_group.source,
                        )
                    )
            recharge_multiplier, recharge_source, recharge_status = _discover_recharge_multiplier(
                active_type,
                usable,
            )
            balance = _discover_balance(
                active_type,
                responses,
                access_token=access_token,
                new_api_user=new_api_user,
            )
            today_balance_used, today_balance_unit, today_balance_status = (
                _discover_today_balance_usage(
                    active_type,
                    responses,
                    access_token=access_token,
                    new_api_user=new_api_user,
                )
            )
            yesterday_balance_used, yesterday_balance_unit, yesterday_balance_status = (
                _discover_yesterday_balance_usage(
                    active_type,
                    responses,
                    access_token=access_token,
                    new_api_user=new_api_user,
                )
            )
            (
                channel_monitors,
                channel_monitors_total,
                channel_monitors_status,
                channel_monitors_message,
            ) = (
                _discover_channel_monitors(
                    active_type,
                    responses,
                    access_token=access_token,
                    secrets=secrets,
                    detail_results=sub2api_channel_monitor_details,
                )
            )
            today_balance_error = _daily_usage_error_detail(
                active_type,
                responses,
                period="today",
                status=today_balance_status,
            )
            yesterday_balance_error = _daily_usage_error_detail(
                active_type,
                responses,
                period="yesterday",
                status=yesterday_balance_status,
            )
        except Exception:
            return safe(_error_result(active_type, source, "Could not parse a valid upstream response."))

        group_multiplier = matched_group.multiplier if matched_group is not None else None
        group_source = matched_group.source if group_multiplier is not None else None
        return safe(
            DiscoveryResult(
                upstream_type=active_type,
                source=source,
                status="ok",
                groups=groups,
                matched_group=matched_group,
                account_group_matches=account_group_matches,
                matched_account_state=matched_account_state,
                account_upstream_states=account_upstream_states,
                discovered_group_multiplier=group_multiplier,
                discovered_group_multiplier_source=group_source,
                discovered_recharge_multiplier=recharge_multiplier,
                discovered_recharge_multiplier_source=recharge_source,
                recharge_discovery_status=recharge_status,
                balance_remaining=balance.remaining,
                balance_total=balance.total,
                balance_used=balance.used,
                balance_unit=balance.unit,
                balance_status=balance.status,
                balance_message=balance.message,
                today_balance_used=today_balance_used,
                today_balance_unit=today_balance_unit,
                today_balance_status=today_balance_status,
                today_balance_error=today_balance_error,
                yesterday_balance_used=yesterday_balance_used,
                yesterday_balance_unit=yesterday_balance_unit,
                yesterday_balance_status=yesterday_balance_status,
                yesterday_balance_error=yesterday_balance_error,
                channel_monitors=channel_monitors,
                channel_monitors_total=channel_monitors_total,
                channel_monitors_status=channel_monitors_status,
                channel_monitors_message=channel_monitors_message,
                sub2api_auth_rejected=sub2api_auth_rejected,
                message=_success_message(group_multiplier, recharge_multiplier),
            )
        )

    async def _reveal_api_key_records(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        upstream_type: str,
        payloads: dict[str, Any],
        target_keys: set[str],
        access_token: str | None,
        new_api_user: str | int | None,
    ) -> dict[str, dict[str, Any]]:
        token = _clean_secret(access_token)
        if token is None or not target_keys:
            return {}
        if upstream_type == "newapi":
            user_id = _clean_new_api_user(new_api_user)
            if user_id is None:
                return {}
            headers = _build_headers(
                access_token=token,
                api_key=None,
                new_api_user=user_id,
                raw_authorization=True,
            )
        elif upstream_type == "sub2api":
            headers = _build_headers(
                access_token=token,
                api_key=None,
                new_api_user=None,
            )
        else:
            return {}

        pending_keys = {
            key
            for key in target_keys
            if _find_unique_api_key_record(upstream_type, payloads, key) is None
        }
        if not pending_keys:
            return {}

        candidate_records_by_id: dict[int, dict[str, Any]] = {}
        candidate_ids_by_key: dict[str, dict[int, None]] = {
            target_key: {}
            for target_key in pending_keys
        }
        fallback_records_by_id: dict[int, dict[str, Any]] = {}
        for record in _iter_api_key_records(upstream_type, payloads):
            listed_key = _clean_secret(
                _first_value(record, ("key", "api_key", "apiKey", "token", "value"))
            )
            if listed_key is not None and any(
                target_key not in pending_keys
                and _api_keys_equal(upstream_type, target_key, listed_key)
                for target_key in target_keys
            ):
                continue
            record_id = _positive_int64(
                _first_value(record, ("id", "token_id", "tokenId", "key_id", "keyId"))
            )
            if record_id is not None:
                matching_targets = [
                    target_key
                    for target_key in pending_keys
                    if listed_key is not None
                    and _masked_api_key_matches(listed_key, target_key)
                ]
                if matching_targets:
                    candidate_records_by_id.setdefault(record_id, record)
                    for target_key in matching_targets:
                        candidate_ids_by_key[target_key].setdefault(record_id, None)
                else:
                    fallback_records_by_id.setdefault(record_id, record)

        records_by_id: dict[int, dict[str, Any]] = {}
        deferred_candidate_ids: list[list[int]] = []
        # Resolve small ambiguous sets completely before sharing the remaining
        # bounded reveal budget across sets that cannot fit in full.
        ordered_candidates = sorted(
            candidate_ids_by_key.items(),
            key=lambda item: (len(item[1]), item[0]),
        )
        for _target_key, candidate_ids in ordered_candidates:
            missing_ids = [
                record_id
                for record_id in candidate_ids
                if record_id not in records_by_id
            ]
            remaining_capacity = MAX_AUTOMATIC_KEY_REVEALS - len(records_by_id)
            if len(missing_ids) <= remaining_capacity:
                for record_id in missing_ids:
                    records_by_id[record_id] = candidate_records_by_id[record_id]
            else:
                deferred_candidate_ids.append(missing_ids)

        while deferred_candidate_ids and len(records_by_id) < MAX_AUTOMATIC_KEY_REVEALS:
            next_round: list[list[int]] = []
            for candidate_ids in deferred_candidate_ids:
                while candidate_ids and candidate_ids[0] in records_by_id:
                    candidate_ids.pop(0)
                if candidate_ids:
                    record_id = candidate_ids.pop(0)
                    records_by_id[record_id] = candidate_records_by_id[record_id]
                if candidate_ids:
                    next_round.append(candidate_ids)
                if len(records_by_id) >= MAX_AUTOMATIC_KEY_REVEALS:
                    break
            deferred_candidate_ids = next_round

        for record_id, record in fallback_records_by_id.items():
            if len(records_by_id) >= MAX_AUTOMATIC_KEY_REVEALS:
                break
            records_by_id.setdefault(record_id, record)
        if not records_by_id:
            return {}

        semaphore = asyncio.Semaphore(KEY_REVEAL_CONCURRENCY)

        async def reveal(record_id: int, listed_record: dict[str, Any]):
            endpoint = (
                f"/api/token/{record_id}/key"
                if upstream_type == "newapi"
                else f"/api/v1/keys/{record_id}"
            )
            method = "POST" if upstream_type == "newapi" else "GET"
            async with semaphore:
                response = await self._request_json(
                    client,
                    base_url,
                    endpoint,
                    method=method,
                    headers=headers,
                )
            if not response.ok or not _payload_succeeded(response.payload):
                return None
            data = _unwrap(response.payload)
            if not isinstance(data, dict):
                return None
            returned_id = _api_key_record_id(data)
            if returned_id is not None and returned_id != record_id:
                return None
            revealed_key = _clean_secret(
                _first_value(data, ("key", "api_key", "apiKey", "token", "value"))
            )
            if revealed_key is None or "*" in revealed_key:
                return None
            matching_targets = [
                target_key
                for target_key in pending_keys
                if _api_keys_equal(upstream_type, target_key, revealed_key)
            ]
            if len(matching_targets) != 1:
                return None
            matched_record = (
                {**listed_record, **data}
                if upstream_type == "sub2api"
                else listed_record
            )
            return matching_targets[0], matched_record

        revealed = await asyncio.gather(
            *(reveal(record_id, record) for record_id, record in records_by_id.items()),
            return_exceptions=True,
        )
        candidates: dict[str, list[dict[str, Any]]] = {}
        for item in revealed:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], dict)
            ):
                candidates.setdefault(item[0], []).append(item[1])
        return {
            target_key: records[0]
            for target_key, records in candidates.items()
            if len(records) == 1
        }

    async def _fetch_sub2api_channel_monitor_details(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        list_result: _FetchResult | None,
        access_token: str | None,
    ) -> dict[int, _FetchResult]:
        token = _clean_secret(access_token)
        if (
            token is None
            or list_result is None
            or not list_result.ok
            or not _payload_succeeded(list_result.payload)
        ):
            return {}
        data = _unwrap(list_result.payload)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {}

        monitor_ids: list[int] = []
        seen_ids: set[int] = set()
        for raw in data["items"]:
            monitor_id = _positive_int64(raw.get("id")) if isinstance(raw, dict) else None
            if monitor_id is None or monitor_id in seen_ids:
                continue
            monitor_ids.append(monitor_id)
            seen_ids.add(monitor_id)
            if len(monitor_ids) >= MAX_CHANNEL_MONITORS:
                break
        if not monitor_ids:
            return {}

        headers = _build_headers(
            access_token=token,
            api_key=None,
            new_api_user=None,
        )
        semaphore = asyncio.Semaphore(CHANNEL_MONITOR_DETAIL_CONCURRENCY)

        async def fetch_detail(monitor_id: int) -> tuple[int, _FetchResult]:
            # The only interpolated value has passed strict positive-int64
            # validation, so the request remains on this fixed upstream path.
            endpoint = f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/{monitor_id}/status"
            async with semaphore:
                result = await self._request_json(
                    client,
                    base_url,
                    endpoint,
                    headers=headers,
                )
            return monitor_id, result

        fetched_details = await asyncio.gather(
            *(fetch_detail(monitor_id) for monitor_id in monitor_ids),
            return_exceptions=True,
        )
        details_by_id: dict[int, _FetchResult] = {}
        for item in fetched_details:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], int)
                and isinstance(item[1], _FetchResult)
            ):
                details_by_id[item[0]] = item[1]
        return details_by_id

    async def _fetch_cached_api_key_records(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        upstream_type: str,
        payloads: dict[str, Any],
        record_ids_by_account: Mapping[int, int],
        api_keys_by_account: Mapping[int, str],
        access_token: str | None,
        new_api_user: str | int | None,
    ) -> dict[int, dict[str, Any]]:
        if upstream_type not in {"newapi", "sub2api"} or not record_ids_by_account:
            return {}
        verifiable_record_ids_by_account = {
            account_id: record_id
            for account_id, record_id in record_ids_by_account.items()
            if _clean_secret(api_keys_by_account.get(account_id)) is not None
        }
        if not verifiable_record_ids_by_account:
            return {}
        listed_by_id = {
            record_id: record
            for record in _deduplicated_api_key_records(upstream_type, payloads)
            if (record_id := _api_key_record_id(record)) is not None
        }
        matched = {
            account_id: listed_by_id[record_id]
            for account_id, record_id in verifiable_record_ids_by_account.items()
            if record_id in listed_by_id
            and _api_key_record_matches_account(
                upstream_type,
                listed_by_id[record_id],
                account_id,
                api_keys_by_account,
            )
        }
        missing_record_ids = sorted(
            {
                record_id
                for account_id, record_id in verifiable_record_ids_by_account.items()
                if account_id not in matched
            }
        )
        if not missing_record_ids:
            return matched
        token = _clean_secret(access_token)
        if token is None:
            return matched
        headers = _build_headers(
            access_token=token,
            api_key=None,
            new_api_user=new_api_user if upstream_type == "newapi" else None,
        )
        semaphore = asyncio.Semaphore(KEY_REVEAL_CONCURRENCY)

        async def fetch_record(record_id: int) -> tuple[int, dict[str, Any]] | None:
            endpoint = (
                f"/api/token/{record_id}"
                if upstream_type == "newapi"
                else f"/api/v1/keys/{record_id}"
            )
            async with semaphore:
                result = await self._request_json(
                    client,
                    base_url,
                    endpoint,
                    headers=headers,
                )
            if not result.ok or not _payload_succeeded(result.payload):
                return None
            data = _unwrap(result.payload)
            if not isinstance(data, dict):
                return None
            returned_id = _api_key_record_id(data)
            if returned_id is not None and returned_id != record_id:
                return None
            return record_id, {"id": record_id, **data}

        fetched = await asyncio.gather(
            *(fetch_record(record_id) for record_id in missing_record_ids),
            return_exceptions=True,
        )
        fetched_by_id = {
            record_id: record
            for item in fetched
            if isinstance(item, tuple) and len(item) == 2
            for record_id, record in (item,)
        }
        matched.update(
            {
                account_id: fetched_by_id[record_id]
                for account_id, record_id in verifiable_record_ids_by_account.items()
                if account_id not in matched
                and record_id in fetched_by_id
                and _api_key_record_matches_account(
                    upstream_type,
                    fetched_by_id[record_id],
                    account_id,
                    api_keys_by_account,
                )
            }
        )
        return matched

    async def _fetch_sub2api_api_key_pages(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        payloads: dict[str, Any],
        target_keys: set[str],
        access_token: str | None,
    ) -> dict[str, Any]:
        token = _clean_secret(access_token)
        if token is None or not target_keys:
            return {}
        source = _sub2api_api_key_page_source(payloads)
        if source is None:
            return {}
        source_path, page_count = source
        last_page = min(page_count, MAX_SUB2API_API_KEY_PAGES)
        if last_page <= 1:
            return {}
        if _all_target_api_keys_listed_exactly("sub2api", payloads, target_keys):
            return {}

        headers = _build_headers(
            access_token=token,
            api_key=None,
            new_api_user=None,
        )
        semaphore = asyncio.Semaphore(SUB2API_API_KEY_PAGE_CONCURRENCY)

        async def fetch_page(page: int) -> tuple[str, Any] | None:
            endpoint = (
                f"{source_path}?page={page}&page_size={SUB2API_API_KEY_PAGE_SIZE}"
            )
            async with semaphore:
                result = await self._request_json(
                    client,
                    base_url,
                    endpoint,
                    headers=headers,
                )
            if not result.ok or not _payload_succeeded(result.payload):
                return None
            data = _unwrap(result.payload)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return None
            returned_page = _positive_int64(data.get("page"))
            returned_page_size = _positive_int64(data.get("page_size"))
            if (
                returned_page != page
                or returned_page_size != SUB2API_API_KEY_PAGE_SIZE
            ):
                return None
            return endpoint, result.payload

        fetched_pages = await asyncio.gather(
            *(fetch_page(page) for page in range(2, last_page + 1)),
            return_exceptions=True,
        )
        payloads_by_endpoint: dict[str, Any] = {}
        for item in fetched_pages:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
            ):
                payloads_by_endpoint[item[0]] = item[1]
        return payloads_by_endpoint

    async def _fetch_sub2api_api_key_usage(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        payloads: dict[str, Any],
        revealed_records: Mapping[str, dict[str, Any]],
        target_keys: set[str],
        access_token: str | None,
    ) -> dict[str, float]:
        token = _clean_secret(access_token)
        if token is None or not target_keys:
            return {}

        records = _matched_target_api_key_records(
            "sub2api",
            payloads,
            target_keys,
            revealed_records,
        )
        keys_by_record_id: dict[int, list[str]] = {}
        for target_key, record in records.items():
            record_id = _positive_int64(
                _first_value(record, ("id", "token_id", "tokenId", "key_id", "keyId"))
            )
            if record_id is not None:
                keys_by_record_id.setdefault(record_id, []).append(target_key)

        # One upstream record must identify exactly one distinct target key.
        # Ambiguous matches are omitted instead of duplicating usage across keys.
        key_by_record_id = {
            record_id: keys[0]
            for record_id, keys in keys_by_record_id.items()
            if len(keys) == 1
        }
        record_ids = sorted(key_by_record_id)
        if not record_ids:
            return {}

        usage_by_record_id = await self._fetch_sub2api_usage_by_record_ids(
            client,
            base_url,
            record_ids=set(record_ids),
            access_token=token,
        )
        return {
            key_by_record_id[record_id]: amount
            for record_id, amount in usage_by_record_id.items()
            if record_id in key_by_record_id
        }

    async def _fetch_sub2api_usage_by_record_ids(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        record_ids: set[int],
        access_token: str | None,
    ) -> dict[int, float]:
        token = _clean_secret(access_token)
        if token is None or not record_ids:
            return {}
        normalized_record_ids = sorted(
            record_id
            for value in record_ids
            if (record_id := _positive_int64(value)) is not None
        )
        if not normalized_record_ids:
            return {}
        headers = _build_headers(access_token=token, api_key=None, new_api_user=None)
        batches = [
            normalized_record_ids[index : index + SUB2API_API_KEY_USAGE_BATCH_SIZE]
            for index in range(0, len(normalized_record_ids), SUB2API_API_KEY_USAGE_BATCH_SIZE)
        ]
        results = await asyncio.gather(
            *(
                self._request_json(
                    client,
                    base_url,
                    SUB2API_API_KEY_USAGE_ENDPOINT,
                    method="POST",
                    headers=headers,
                    json_body={"api_key_ids": batch},
                    timeout_seconds=max(
                        self.timeout_seconds,
                        UPSTREAM_USAGE_TIMEOUT_SECONDS,
                    ),
                )
                for batch in batches
            )
        )

        usage_by_record_id: dict[int, float] = {}
        for batch, result in zip(batches, results):
            usage_by_record_id.update(
                _parse_sub2api_api_key_usage_batch(result, expected_ids=set(batch))
            )
        return usage_by_record_id

    async def refresh_sub2api_tokens(
        self,
        base_url: str,
        refresh_token: str,
    ) -> Sub2ApiTokenPair | None:
        """Exchange one single-use Sub2API refresh token without exposing it."""

        token = _clean_secret(refresh_token)
        if token is None or len(token) > MAX_UPSTREAM_TOKEN_LENGTH:
            return None
        try:
            normalized_url, hostname = _normalize_base_url(base_url)
            if urlparse(normalized_url).scheme != "https":
                return None
            pinned_addresses = await self._resolve_public_addresses(hostname)
        except Exception:
            return None

        # Sub2API rotates refresh tokens immediately. A state-changing request
        # must never be replayed against another DNS answer after an ambiguous
        # timeout, so use exactly one validated address.
        pinned_transport = _PinnedAsyncTransport(
            hostname=hostname,
            address=pinned_addresses[0],
            inner=self.transport,
            close_inner=self.transport is None,
        )
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                headers={
                    "Accept": "application/json",
                    "User-Agent": "gpt-check-upstream-discovery/1.0",
                },
                timeout=timeout,
                transport=pinned_transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                result = await self._request_json(
                    client,
                    normalized_url,
                    SUB2API_REFRESH_ENDPOINT,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                    json_body={"refresh_token": token},
                )
        except Exception:
            return None
        finally:
            if self.transport is not None:
                await self.transport.aclose()

        if not result.ok or not _payload_succeeded(result.payload):
            return None
        payload = _unwrap(result.payload)
        if not isinstance(payload, dict):
            return None
        access_token = _clean_secret(payload.get("access_token"))
        rotated_refresh_token = _clean_secret(payload.get("refresh_token"))
        if (
            access_token is None
            or rotated_refresh_token is None
            or len(access_token) > MAX_UPSTREAM_TOKEN_LENGTH
            or len(rotated_refresh_token) > MAX_UPSTREAM_TOKEN_LENGTH
        ):
            return None
        try:
            _validate_header_value(access_token)
        except ValueError:
            return None
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
            expires_in = None
        return Sub2ApiTokenPair(
            access_token=access_token,
            refresh_token=rotated_refresh_token,
            expires_in=expires_in,
        )

    async def _resolve_public_addresses(self, hostname: str) -> tuple[str, ...]:
        lowered = hostname.rstrip(".").casefold()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise ValueError("local host is not allowed")

        try:
            direct_ip = ipaddress.ip_address(lowered)
        except ValueError:
            direct_ip = None
        if direct_ip is not None:
            if not direct_ip.is_global:
                raise ValueError("non-public address is not allowed")
            return (str(direct_ip),)

        if self.cache_dns:
            cached = _cached_dns_addresses(lowered)
            if cached is not None:
                return cached

        resolved = self.resolver(lowered)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        addresses = list(resolved)
        if not addresses:
            raise OSError("hostname did not resolve")
        validated: list[str] = []
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(str(raw_address).strip())
            except ValueError as exc:
                raise OSError("resolver returned an invalid address") from exc
            if not address.is_global:
                raise ValueError("non-public address is not allowed")
            normalized = str(address)
            if normalized not in validated:
                validated.append(normalized)
        result = tuple(validated[:MAX_PINNED_ADDRESSES])
        if self.cache_dns:
            _store_dns_addresses(lowered, result)
        return result

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        endpoint: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> _FetchResult:
        request_timeout = httpx.Timeout(
            timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        )
        try:
            async with client.stream(
                method,
                f"{base_url}{endpoint}",
                headers=headers,
                params=params,
                json=json_body,
                timeout=request_timeout,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    # Do not consume or echo upstream error bodies.  Redirects
                    # are errors too and follow_redirects is disabled.
                    return _FetchResult(
                        ok=False,
                        status_code=response.status_code,
                        error_kind="http_status",
                    )

                declared_length = _content_length(response.headers.get("content-length"))
                if declared_length is not None and declared_length > self.max_response_bytes:
                    return _FetchResult(ok=False, status_code=response.status_code, error_kind="too_large")

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        return _FetchResult(ok=False, status_code=response.status_code, error_kind="too_large")
                    chunks.append(chunk)
        except httpx.TimeoutException:
            return _FetchResult(ok=False, error_kind="timeout")
        except httpx.HTTPError:
            return _FetchResult(ok=False, error_kind="network")

        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _FetchResult(ok=False, status_code=200, error_kind="invalid_json")
        return _FetchResult(ok=True, status_code=200, payload=payload)


# Descriptive alias retained for service code that wants to make the purpose
# explicit without introducing a second implementation.
UpstreamDiscoveryClient = UpstreamClient


async def discover_upstream(
    base_url: str,
    *,
    upstream_type: UpstreamType | str = "auto",
    api_key: str | None = None,
    access_token: str | None = None,
    new_api_user: str | int | None = None,
    selected_group_id: str | int | None = None,
    selected_group_name: str | None = None,
    account_api_keys: Mapping[int | str, str] | None = None,
    account_api_key_record_ids: Mapping[int | str, int | str] | None = None,
    optimized_endpoint_fallbacks: bool = False,
    include_channel_monitors: bool = True,
    include_channel_monitor_details: bool = False,
    monitor_only: bool = False,
    today_timezone: str = DEFAULT_TODAY_TIME_ZONE,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> DiscoveryResult:
    """Convenience wrapper around :class:`UpstreamClient`."""

    client = UpstreamClient(
        timeout_seconds=timeout_seconds,
        transport=transport,
        resolver=resolver,
    )
    return await client.discover(
        base_url,
        upstream_type=upstream_type,
        api_key=api_key,
        access_token=access_token,
        new_api_user=new_api_user,
        selected_group_id=selected_group_id,
        selected_group_name=selected_group_name,
        account_api_keys=account_api_keys,
        account_api_key_record_ids=account_api_key_record_ids,
        optimized_endpoint_fallbacks=optimized_endpoint_fallbacks,
        include_channel_monitors=include_channel_monitors,
        include_channel_monitor_details=include_channel_monitor_details,
        monitor_only=monitor_only,
        today_timezone=today_timezone,
    )


async def refresh_sub2api_tokens(
    base_url: str,
    refresh_token: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> Sub2ApiTokenPair | None:
    """Exchange a Sub2API refresh token through the hardened pinned client."""

    client = UpstreamClient(
        timeout_seconds=timeout_seconds,
        transport=transport,
        resolver=resolver,
    )
    return await client.refresh_sub2api_tokens(base_url, refresh_token)


def _normalize_base_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise TypeError("URL must be a string")
    text = value.strip()
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("invalid URL")

    parsed = urlparse(text)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("invalid URL scheme")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("URL userinfo is not allowed")
    if parsed.query or parsed.fragment or parsed.params:
        raise ValueError("URL query and fragment are not allowed")
    if "\\" in parsed.netloc:
        raise ValueError("invalid URL authority")

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL authority") from exc
    if not hostname:
        raise ValueError("URL host is required")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("invalid URL host") from exc

    path = parsed.path.rstrip("/")
    lowered_path = path.casefold()
    if lowered_path.endswith("/api/v1"):
        path = path[: -len("/api/v1")].rstrip("/")
    elif lowered_path.endswith("/v1"):
        path = path[: -len("/v1")].rstrip("/")
    if path == "/":
        path = ""

    host_for_url = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    netloc = f"{host_for_url}:{port}" if port is not None else host_for_url
    normalized = urlunparse((parsed.scheme.casefold(), netloc, path, "", "", "")).rstrip("/")
    return normalized, ascii_hostname


async def _default_resolver(hostname: str) -> Sequence[str]:
    def resolve() -> list[str]:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(record[4][0] for record in records))

    addresses = await asyncio.to_thread(resolve)
    parsed_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for address in addresses:
        try:
            parsed_addresses.append(ipaddress.ip_address(address))
        except ValueError:
            return addresses
    if parsed_addresses and all(address in FAKE_IP_NETWORK for address in parsed_addresses):
        doh_addresses = await _doh_resolver(hostname)
        if doh_addresses:
            return doh_addresses
    return addresses


async def _doh_resolver(hostname: str) -> list[str]:
    """Resolve proxy fake-IP answers through one fixed public DoH service."""

    timeout = httpx.Timeout(DEFAULT_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(
            headers={"Accept": "application/dns-json"},
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async def query(record_type: str) -> list[str]:
                try:
                    async with client.stream(
                        "GET",
                        DNS_OVER_HTTPS_URL,
                        params={"name": hostname, "type": record_type},
                    ) as response:
                        if response.status_code != 200:
                            return []
                        declared_length = _content_length(response.headers.get("content-length"))
                        if declared_length is not None and declared_length > MAX_DOH_RESPONSE_BYTES:
                            return []
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_DOH_RESPONSE_BYTES:
                                return []
                            chunks.append(chunk)
                    payload = json.loads(b"".join(chunks))
                except (httpx.HTTPError, json.JSONDecodeError, ValueError):
                    return []
                if not isinstance(payload, dict) or payload.get("Status") not in (0, "0"):
                    return []
                answers = payload.get("Answer")
                if not isinstance(answers, list):
                    return []
                addresses: list[str] = []
                for answer in answers:
                    if not isinstance(answer, dict):
                        continue
                    raw_address = answer.get("data")
                    try:
                        normalized = str(ipaddress.ip_address(str(raw_address)))
                    except ValueError:
                        continue
                    if normalized not in addresses:
                        addresses.append(normalized)
                return addresses

            resolved = await asyncio.gather(query("A"), query("AAAA"))
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return []
    return list(dict.fromkeys(address for answers in resolved for address in answers))


def _cached_dns_addresses(hostname: str) -> tuple[str, ...] | None:
    now = time.monotonic()
    with _DNS_CACHE_LOCK:
        cached = _DNS_CACHE.get(hostname)
        if cached is None:
            return None
        expires_at, addresses = cached
        if expires_at <= now:
            _DNS_CACHE.pop(hostname, None)
            return None
        return addresses


def _store_dns_addresses(hostname: str, addresses: tuple[str, ...]) -> None:
    if not addresses:
        return
    with _DNS_CACHE_LOCK:
        _DNS_CACHE.pop(hostname, None)
        while len(_DNS_CACHE) >= MAX_DNS_CACHE_ENTRIES:
            _DNS_CACHE.pop(next(iter(_DNS_CACHE)))
        _DNS_CACHE[hostname] = (
            time.monotonic() + DNS_CACHE_TTL_SECONDS,
            addresses,
        )


def _invalidate_dns_cache(hostname: str) -> None:
    with _DNS_CACHE_LOCK:
        _DNS_CACHE.pop(hostname.rstrip(".").casefold(), None)


def _build_headers(
    *,
    access_token: str | None,
    api_key: str | None,
    new_api_user: str | int | None,
    raw_authorization: bool = False,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "gpt-check-upstream-discovery/1.0",
    }
    token = _clean_secret(access_token) or _clean_secret(api_key)
    if token:
        if token.casefold().startswith("bearer "):
            token = token[7:].strip()
        _validate_header_value(token)
        headers["Authorization"] = token if raw_authorization else f"Bearer {token}"
    if new_api_user is not None and str(new_api_user).strip():
        user_value = str(new_api_user).strip()
        _validate_header_value(user_value)
        headers["New-Api-User"] = user_value
    return headers


def _headers_for_endpoint(
    endpoint: str,
    *,
    requested_type: str,
    access_token: str | None,
    api_key: str | None,
    new_api_user: str | int | None,
) -> dict[str, str] | None:
    # These are public NewAPI endpoints. Do not send either stored credential
    # to them even though every request remains pinned to the validated origin.
    if endpoint in {"/api/status", NEWAPI_UPTIME_STATUS_ENDPOINT}:
        return {}

    if endpoint in {NEWAPI_BALANCE_ENDPOINT, NEWAPI_TODAY_USAGE_ENDPOINT}:
        user_id = _clean_new_api_user(new_api_user)
        if _clean_secret(access_token) is None or user_id is None:
            return None
        return _build_headers(
            access_token=access_token,
            api_key=None,
            new_api_user=user_id,
            raw_authorization=True,
        )

    if endpoint in {
        SUB2API_BALANCE_ENDPOINT,
        SUB2API_TODAY_USAGE_ENDPOINT,
        SUB2API_USAGE_STATS_ENDPOINT,
        SUB2API_API_KEY_USAGE_ENDPOINT,
        SUB2API_CHANNEL_MONITORS_ENDPOINT,
    }:
        if _clean_secret(access_token) is None:
            return None
        return _build_headers(
            access_token=access_token,
            api_key=None,
            new_api_user=None,
        )

    newapi_endpoint = endpoint in NEWAPI_ENDPOINTS and endpoint not in SUB2API_ENDPOINTS
    return _build_headers(
        access_token=access_token,
        api_key=api_key,
        new_api_user=new_api_user if requested_type != "sub2api" else None,
        raw_authorization=bool(
            _clean_secret(access_token)
            and (requested_type == "newapi" or (requested_type == "auto" and newapi_endpoint))
        ),
    )


def _validate_header_value(value: str) -> None:
    if len(value) > 16_384 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid header value")


def _clean_secret(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_new_api_user(value: str | int | None) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    cleaned = str(value).strip()
    if not cleaned or not cleaned.isascii() or not cleaned.isdigit():
        return None
    try:
        return cleaned if int(cleaned) > 0 else None
    except ValueError:
        return None


def _secret_variants(secrets: Iterable[str | None]) -> tuple[str, ...]:
    values: set[str] = set()
    for secret in secrets:
        cleaned = _clean_secret(secret)
        if not cleaned:
            continue
        values.add(cleaned)
        if cleaned.casefold().startswith("bearer ") and cleaned[7:].strip():
            values.add(cleaned[7:].strip())
    return tuple(sorted(values, key=len, reverse=True))


def _scrub_text(value: Any, secrets: Iterable[str | None], maximum: int) -> str | None:
    return _scrub_text_with_variants(
        value,
        _secret_variants(secrets),
        maximum,
    )


def _scrub_text_with_variants(
    value: Any,
    secret_variants: Sequence[str],
    maximum: int,
) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value)
    for secret in secret_variants:
        text = text.replace(secret, "[redacted]")
    return _clean_text(text, maximum)


def _scrub_group_option(option: GroupOption, secrets: Iterable[str | None]) -> GroupOption:
    return replace(
        option,
        id=_scrub_text(option.id, secrets, 160) or "[redacted]",
        name=_scrub_text(option.name, secrets, 160) or "[redacted]",
        description=_scrub_text(option.description, secrets, 500),
        source=_scrub_text(option.source, secrets, 160),
    )


def _scrub_account_group_match(
    match: AccountGroupMatch,
    secrets: Iterable[str | None],
) -> AccountGroupMatch:
    return replace(
        match,
        id=_scrub_text(match.id, secrets, 160) or "[redacted]",
        name=_scrub_text(match.name, secrets, 160) or "[redacted]",
        source=_scrub_text(match.source, secrets, 160),
    )


def _scrub_account_upstream_state(
    state: AccountUpstreamState,
    secrets: Iterable[str | None],
) -> AccountUpstreamState:
    return replace(
        state,
        group_id=_scrub_text(state.group_id, secrets, 160),
        group_name=_scrub_text(state.group_name, secrets, 160),
    )


def _scrub_channel_monitor(
    monitor: dict[str, Any],
    secret_variants: Sequence[str],
) -> dict[str, Any] | None:
    return _sanitize_channel_monitor(
        monitor,
        secret_variants=secret_variants,
    )


def _scrub_discovery_result(
    result: DiscoveryResult,
    secrets: Iterable[str | None],
) -> DiscoveryResult:
    secret_values = tuple(secrets)
    secret_variants = _secret_variants(secret_values)
    scrubbed_groups = [
        _scrub_group_option(group, secret_values)
        for group in result.groups
    ]
    scrubbed_match = (
        _scrub_group_option(result.matched_group, secret_values)
        if result.matched_group is not None
        else None
    )
    scrubbed_account_matches = {
        account_id: _scrub_account_group_match(group, secret_values)
        for account_id, group in result.account_group_matches.items()
    }
    scrubbed_account_state = (
        _scrub_account_upstream_state(result.matched_account_state, secret_values)
        if result.matched_account_state is not None
        else None
    )
    scrubbed_account_states = {
        account_id: _scrub_account_upstream_state(state, secret_values)
        for account_id, state in result.account_upstream_states.items()
    }
    scrubbed_channel_monitors = [
        scrubbed_monitor
        for monitor in result.channel_monitors
        if isinstance(monitor, dict)
        if (
            scrubbed_monitor := _scrub_channel_monitor(monitor, secret_variants)
        ) is not None
    ]
    return replace(
        result,
        groups=scrubbed_groups,
        matched_group=scrubbed_match,
        account_group_matches=scrubbed_account_matches,
        matched_account_state=scrubbed_account_state,
        account_upstream_states=scrubbed_account_states,
        channel_monitors=scrubbed_channel_monitors,
        channel_monitors_message=(
            _scrub_text(result.channel_monitors_message, secret_values, 300) or ""
        ),
        discovered_group_multiplier_source=_scrub_text(
            result.discovered_group_multiplier_source,
            secret_values,
            160,
        ),
        discovered_recharge_multiplier_source=_scrub_text(
            result.discovered_recharge_multiplier_source,
            secret_values,
            160,
        ),
        balance_message=_scrub_text(result.balance_message, secret_values, 300) or "",
        today_balance_error=_scrub_text(
            result.today_balance_error,
            secret_values,
            80,
        ),
        yesterday_balance_error=_scrub_text(
            result.yesterday_balance_error,
            secret_values,
            80,
        ),
        message=_scrub_text(result.message, secret_values, 500) or "Upstream discovery completed.",
    )


def _clean_upstream_type(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().casefold()
    return cleaned if cleaned in {"auto", "newapi", "sub2api"} else None


def _ordered_union(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _missing_compatibility_endpoints(
    upstream_type: str,
    responses: dict[str, _FetchResult],
) -> tuple[str, ...]:
    payloads = {
        endpoint: result.payload
        for endpoint, result in responses.items()
        if result.ok and _payload_succeeded(result.payload)
    }
    fallbacks: list[str] = []
    try:
        groups_missing = not _discover_groups(upstream_type, payloads)
    except Exception:
        groups_missing = True
    if upstream_type == "newapi" and groups_missing:
        fallbacks.append("/api/v1/groups/available")

    if not any(_iter_api_key_records(upstream_type, payloads)):
        if upstream_type == "newapi":
            fallbacks.extend(
                (
                    "/api/token/search?p=1&size=200",
                    "/api/v1/keys?page=1&page_size=200",
                )
            )
        else:
            fallbacks.append("/api/v1/api-keys?page=1&page_size=200")

    try:
        _recharge, _source, recharge_status = _discover_recharge_multiplier(
            upstream_type,
            payloads,
        )
    except Exception:
        recharge_status = "error"
    if recharge_status != "ok":
        fallbacks.append("/api/v1/payment/config")
        if upstream_type == "newapi":
            fallbacks.append("/api/status")
    return tuple(
        endpoint
        for endpoint in _ordered_union(fallbacks)
        if endpoint not in responses
    )


def _payload_succeeded(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    success = payload.get("success")
    if success is False or (isinstance(success, str) and success.casefold() == "false"):
        return False
    if "code" in payload:
        code = payload.get("code")
        if code not in (None, 0, "0", 200, "200", "success", "SUCCESS"):
            return False
    return True


def _unwrap(payload: Any) -> Any:
    current = payload
    for _ in range(8):
        if not isinstance(current, dict):
            break
        envelope_keys = {"success", "code", "message", "msg", "error", "status", "data", "result"}
        next_value: Any = None
        if "data" in current and (
            any(key in current for key in ("success", "code", "message", "msg", "error"))
            or set(current).issubset(envelope_keys)
        ):
            next_value = current.get("data")
        elif "result" in current and (
            any(key in current for key in ("success", "code", "message", "msg", "error"))
            or set(current).issubset(envelope_keys)
        ):
            next_value = current.get("result")
        else:
            break
        if next_value is current:
            break
        current = next_value
    return current


def _detect_upstream_type(responses: dict[str, _FetchResult]) -> str | None:
    usable = {
        endpoint: result.payload
        for endpoint, result in responses.items()
        if result.ok and _payload_succeeded(result.payload)
    }
    if not usable:
        return None

    newapi_score = 0
    sub2api_score = 0
    for endpoint in usable:
        bare_path = endpoint.split("?", 1)[0]
        if bare_path in {
            "/api/user/self/groups",
            "/api/pricing",
            "/api/user/self",
            "/api/token/",
            "/api/token/search",
            NEWAPI_UPTIME_STATUS_ENDPOINT,
        }:
            newapi_score += 3
        elif bare_path == "/api/status":
            status_payload = _unwrap(usable[endpoint])
            if isinstance(status_payload, dict) and any(
                key in status_payload for key in ("price", "quota_per_unit", "system_name", "version")
            ):
                newapi_score += 3
        elif bare_path in {
            "/api/v1/groups/rates",
            "/api/v1/api-keys",
            SUB2API_BALANCE_ENDPOINT,
            SUB2API_USAGE_STATS_ENDPOINT,
            SUB2API_CHANNEL_MONITORS_ENDPOINT,
        }:
            sub2api_score += 4

    available = usable.get("/api/v1/groups/available")
    if available is not None:
        group_items = _group_items(available, allow_direct_map=True)
        if any(isinstance(item, dict) and "rate_multiplier" in item for _, item in group_items):
            sub2api_score += 2
        elif group_items:
            newapi_score += 1

    if sub2api_score > newapi_score:
        return "sub2api"
    if newapi_score > 0:
        return "newapi"
    if sub2api_score > 0 or available is not None:
        return "sub2api"
    return None


def _discover_groups(upstream_type: str, payloads: dict[str, Any]) -> list[GroupOption]:
    groups: list[GroupOption] = []
    if upstream_type == "newapi":
        group_endpoints = (
            ("/api/user/self/groups", "self.groups", True),
            ("/api/pricing", "pricing.groups", False),
            ("/api/v1/groups/available", "groups.available", True),
        )
    else:
        group_endpoints = (("/api/v1/groups/available", "groups.available", True),)

    for endpoint, source, allow_direct_map in group_endpoints:
        if endpoint not in payloads:
            continue
        parsed = _parse_groups(payloads[endpoint], source, allow_direct_map=allow_direct_map)
        groups = _merge_groups(groups, parsed)

    if upstream_type == "sub2api" and "/api/v1/groups/rates" in payloads:
        groups = _apply_rate_overrides(groups, payloads["/api/v1/groups/rates"])
    return groups


def _parse_groups(payload: Any, source: str, *, allow_direct_map: bool) -> list[GroupOption]:
    out: list[GroupOption] = []
    for map_key, raw in _group_items(payload, allow_direct_map=allow_direct_map):
        if isinstance(raw, dict):
            raw_id = _first_value(raw, ("id", "group_id", "groupId"))
            raw_name = _first_value(raw, ("name", "group_name", "groupName"))
            if raw_name is None and map_key is not None and not str(map_key).isdigit():
                raw_name = map_key
            if raw_id is None and map_key is not None:
                raw_id = map_key
            multiplier, multiplier_field = _first_positive_field(
                raw,
                ("rate_multiplier", "ratio", "multiplier", "group_multiplier", "rate"),
            )
            description = _clean_text(_first_value(raw, ("description", "desc")), 500)
        else:
            raw_id = map_key if map_key is not None and str(map_key).isdigit() else None
            raw_name = map_key
            multiplier = _positive_number(raw)
            multiplier_field = "ratio" if multiplier is not None else None
            description = None

        group_id = _clean_identifier(raw_id)
        name = _clean_text(raw_name, 160)
        if not name:
            name = group_id or ""
        if not group_id:
            group_id = name
        # The account-management response schema only exposes actionable
        # groups.  NewAPI's special "auto" group commonly has a non-numeric
        # ratio and is intentionally omitted, matching upstream-ops.
        if not name or not group_id or multiplier is None:
            continue
        out.append(
            GroupOption(
                id=group_id,
                name=name,
                description=description,
                multiplier=multiplier,
                source=f"{source}.{multiplier_field}" if multiplier_field else None,
            )
        )
    return out


def _group_items(payload: Any, *, allow_direct_map: bool) -> list[tuple[str | None, Any]]:
    current = _unwrap(payload)
    if isinstance(current, list):
        return [(None, item) for item in current]
    if not isinstance(current, dict):
        return []

    for key in (
        "groups",
        "available_groups",
        "group_options",
        "group_ratio",
        "group_ratios",
        "group_rates",
        "items",
        "records",
        "list",
    ):
        child = current.get(key)
        if isinstance(child, (dict, list)):
            return _group_items(child, allow_direct_map=True)

    if any(key in current for key in ("id", "group_id", "name", "group_name")) and any(
        key in current for key in ("rate_multiplier", "ratio", "multiplier", "group_multiplier", "rate")
    ):
        return [(None, current)]
    if not allow_direct_map:
        return []
    return [(str(key), value) for key, value in current.items() if isinstance(value, (dict, int, float, str))]


def _merge_groups(existing: list[GroupOption], incoming: list[GroupOption]) -> list[GroupOption]:
    result = list(existing)
    for candidate in incoming:
        match_index = next(
            (
                index
                for index, current in enumerate(result)
                if current.id == candidate.id
            ),
            None,
        )
        if match_index is None:
            result.append(candidate)
            continue
        current = result[match_index]
        result[match_index] = GroupOption(
            id=current.id or candidate.id,
            name=current.name or candidate.name,
            description=current.description or candidate.description,
            multiplier=current.multiplier,
            source=current.source,
        )
    return result


def _apply_rate_overrides(groups: list[GroupOption], payload: Any) -> list[GroupOption]:
    overrides = _parse_rate_overrides(payload)
    if not overrides:
        return groups
    result = list(groups)
    for identifier, name, multiplier in overrides:
        for index, group in enumerate(result):
            id_matches = identifier is not None and group.id == identifier
            name_matches = (
                identifier is None
                and name is not None
                and group.name.casefold() == name.casefold()
            )
            if id_matches or name_matches:
                result[index] = replace(group, multiplier=multiplier, source="groups.rates")
                break
    return result


def _parse_rate_overrides(payload: Any) -> list[tuple[str | None, str | None, float]]:
    current = _unwrap(payload)
    if isinstance(current, dict):
        for key in ("rates", "group_rates", "groups", "items", "records", "list"):
            if isinstance(current.get(key), (dict, list)):
                current = current[key]
                break

    out: list[tuple[str | None, str | None, float]] = []
    if isinstance(current, dict):
        for key, raw in current.items():
            if isinstance(raw, dict):
                identifier = _clean_identifier(_first_value(raw, ("group_id", "groupId", "id")))
                name = _clean_text(_first_value(raw, ("group_name", "groupName", "name")), 160)
                multiplier, _ = _first_positive_field(
                    raw,
                    ("rate_multiplier", "ratio", "multiplier", "group_multiplier", "rate"),
                )
            else:
                identifier = _clean_identifier(key) if str(key).isdigit() else None
                name = str(key) if identifier is None else None
                multiplier = _positive_number(raw)
            if identifier is None and str(key).isdigit():
                identifier = str(key)
            if name is None and identifier is None:
                name = _clean_text(key, 160)
            if multiplier is not None:
                out.append((identifier, name, multiplier))
    elif isinstance(current, list):
        for raw in current:
            if not isinstance(raw, dict):
                continue
            multiplier, _ = _first_positive_field(
                raw,
                ("rate_multiplier", "ratio", "multiplier", "group_multiplier", "rate"),
            )
            if multiplier is None:
                continue
            out.append(
                (
                    _clean_identifier(_first_value(raw, ("group_id", "groupId", "id"))),
                    _clean_text(_first_value(raw, ("group_name", "groupName", "name")), 160),
                    multiplier,
                )
            )
    return out


def _canonical_api_key(upstream_type: str, value: str | None) -> str | None:
    cleaned = _clean_secret(value)
    if cleaned is None:
        return None
    if upstream_type == "newapi" and cleaned.startswith("sk-") and len(cleaned) > 3:
        return cleaned[3:]
    return cleaned


def _api_keys_equal(upstream_type: str, left: str | None, right: str | None) -> bool:
    canonical_left = _canonical_api_key(upstream_type, left)
    canonical_right = _canonical_api_key(upstream_type, right)
    return canonical_left is not None and canonical_left == canonical_right


def _all_target_api_keys_listed_exactly(
    upstream_type: str,
    payloads: dict[str, Any],
    target_keys: set[str],
) -> bool:
    if not target_keys:
        return True
    found: set[str] = set()
    for record in _iter_api_key_records(upstream_type, payloads):
        listed_key = _clean_secret(
            _first_value(record, ("api_key", "apiKey", "key", "token", "value"))
        )
        if listed_key is None or "*" in listed_key:
            continue
        for target_key in target_keys - found:
            if _api_keys_equal(upstream_type, listed_key, target_key):
                found.add(target_key)
                break
        if found == target_keys:
            return True
    return False


def _find_api_key_record(upstream_type: str, payloads: dict[str, Any], api_key: str | None) -> dict[str, Any] | None:
    if not api_key:
        return None
    for record in _iter_api_key_records(upstream_type, payloads):
        record_key = _first_value(record, ("api_key", "apiKey", "key", "token", "value"))
        if isinstance(record_key, str) and _api_keys_equal(upstream_type, record_key, api_key):
            return record
    return None


def _find_unique_api_key_record(
    upstream_type: str,
    payloads: dict[str, Any],
    api_key: str | None,
) -> dict[str, Any] | None:
    if not api_key:
        return None
    matches = [
        record
        for record in _deduplicated_api_key_records(upstream_type, payloads)
        if isinstance(
            record_key := _first_value(record, ("api_key", "apiKey", "key", "token", "value")),
            str,
        )
        and _api_keys_equal(upstream_type, record_key, api_key)
    ]
    return matches[0] if len(matches) == 1 else None


def _matched_target_api_key_records(
    upstream_type: str,
    payloads: dict[str, Any],
    target_keys: set[str],
    revealed_records: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    matched: dict[str, dict[str, Any]] = {}
    for target_key in target_keys:
        record = (
            revealed_records.get(target_key)
            or _find_unique_api_key_record(upstream_type, payloads, target_key)
        )
        if record is not None:
            matched[target_key] = record
    return matched


def _sub2api_api_key_page_source(
    payloads: Mapping[str, Any],
) -> tuple[str, int] | None:
    for endpoint in (
        "/api/v1/keys?page=1&page_size=200",
        "/api/v1/api-keys?page=1&page_size=200",
    ):
        payload = payloads.get(endpoint)
        data = _unwrap(payload)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            continue
        returned_page = _positive_int64(data.get("page"))
        page_count = _positive_int64(data.get("pages")) or 1
        if returned_page is not None and returned_page != 1:
            continue
        if (
            page_count > 1
            and _positive_int64(data.get("page_size"))
            != SUB2API_API_KEY_PAGE_SIZE
        ):
            continue
        return endpoint.partition("?")[0], page_count
    return None


def _parse_sub2api_api_key_usage_batch(
    result: _FetchResult,
    *,
    expected_ids: set[int],
) -> dict[int, float]:
    if not result.ok or not _payload_succeeded(result.payload):
        return {}
    data = _unwrap(result.payload)
    if not isinstance(data, dict):
        return {}
    stats = data.get("stats")
    if not isinstance(stats, dict):
        return {}

    parsed: dict[int, float] = {}
    for record_id in expected_ids:
        raw = stats.get(str(record_id))
        if raw is None:
            raw = stats.get(record_id)
        if not isinstance(raw, dict):
            continue
        returned_id = raw.get("api_key_id")
        if returned_id is not None and _positive_int64(returned_id) != record_id:
            continue
        amount = _finite_number(raw.get("today_actual_cost"))
        if amount is None or amount < 0:
            continue
        parsed[record_id] = amount
    return parsed


def _deduplicated_api_key_records(
    upstream_type: str,
    payloads: dict[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int | str]] = set()
    for record in _iter_api_key_records(upstream_type, payloads):
        record_id = _positive_int64(
            _first_value(record, ("id", "token_id", "tokenId", "key_id", "keyId"))
        )
        record_key = _clean_secret(
            _first_value(record, ("key", "api_key", "apiKey", "token", "value"))
        )
        identity: tuple[str, int | str] | None = None
        if record_id is not None:
            identity = ("id", record_id)
        elif record_key is not None:
            identity = ("key", record_key)
        if identity is not None and identity in seen:
            continue
        if identity is not None:
            seen.add(identity)
        records.append(record)
    return records


def _iter_api_key_records(
    upstream_type: str,
    payloads: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    if upstream_type == "newapi":
        endpoints = (
            "/api/token/?p=1&page_size=200",
            "/api/token/search?p=1&size=200",
            "/api/v1/keys?page=1&page_size=200",
        )
    else:
        endpoints = tuple(
            endpoint
            for endpoint in payloads
            if endpoint.partition("?")[0]
            in {"/api/v1/keys", "/api/v1/api-keys"}
        )
    for endpoint in endpoints:
        if endpoint in payloads:
            yield from _extract_key_records(payloads[endpoint])


def _normalize_account_api_keys(
    account_api_keys: Mapping[int | str, str],
) -> dict[int, str]:
    normalized: dict[int, str] = {}
    for raw_account_id, raw_api_key in account_api_keys.items():
        account_id = _positive_int64(raw_account_id)
        api_key = _clean_secret(raw_api_key)
        if (
            account_id is None
            or api_key is None
            or len(api_key) > MAX_UPSTREAM_TOKEN_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
        ):
            continue
        normalized[account_id] = api_key
    return normalized


def _normalize_account_api_key_record_ids(
    account_api_key_record_ids: Mapping[int | str, int | str],
) -> dict[int, int]:
    normalized: dict[int, int] = {}
    for raw_account_id, raw_record_id in account_api_key_record_ids.items():
        account_id = _positive_int64(raw_account_id)
        record_id = _positive_int64(raw_record_id)
        if account_id is not None and record_id is not None:
            normalized[account_id] = record_id
    return normalized


def _api_key_record_id(record: Mapping[str, Any]) -> int | None:
    return _positive_int64(
        _first_value(record, ("id", "token_id", "tokenId", "key_id", "keyId"))
    )


def _api_key_record_matches_account(
    upstream_type: str,
    record: Mapping[str, Any],
    account_id: int,
    api_keys_by_account: Mapping[int, str],
) -> bool:
    normalized_key = _clean_secret(api_keys_by_account.get(account_id))
    if normalized_key is None:
        return False
    record_key = _clean_secret(
        _first_value(record, ("key", "api_key", "apiKey", "token", "value"))
    )
    if record_key is None:
        return False
    if "*" in record_key:
        # Masks only identify detail candidates. They never prove ownership,
        # even when one candidate appears unique in the currently loaded pages.
        return False
    return _api_keys_equal(upstream_type, record_key, normalized_key)


def _available_group_refs(
    upstream_type: str,
    payloads: dict[str, Any],
) -> _AvailableGroupRefs:
    endpoints = (
        ("/api/user/self/groups", "/api/v1/groups/available")
        if upstream_type == "newapi"
        else ("/api/v1/groups/available",)
    )
    authoritative = any(
        endpoint in payloads and isinstance(_unwrap(payloads[endpoint]), (dict, list))
        for endpoint in endpoints
    )
    ids: set[str] = set()
    names: set[str] = set()
    for endpoint in endpoints:
        if endpoint not in payloads:
            continue
        for map_key, raw in _group_items(payloads[endpoint], allow_direct_map=True):
            if isinstance(raw, dict):
                raw_id = _first_value(raw, ("id", "group_id", "groupId"))
                raw_name = _first_value(raw, ("name", "group_name", "groupName"))
                if raw_name is None and map_key is not None and not str(map_key).isdigit():
                    raw_name = map_key
                if raw_id is None and map_key is not None:
                    raw_id = map_key
            else:
                raw_id = map_key if map_key is not None and str(map_key).isdigit() else None
                raw_name = map_key
            group_id = _clean_identifier(raw_id)
            group_name = _clean_text(raw_name, 160)
            if group_id:
                ids.add(group_id.casefold())
            if group_name:
                names.add(group_name.casefold())
    return _AvailableGroupRefs(
        ids=frozenset(ids),
        names=frozenset(names),
        authoritative=authoritative,
    )


def _normalize_api_key_status(upstream_type: str, record: dict[str, Any]) -> str | None:
    for field_name in ("enabled", "is_enabled", "isEnabled", "active", "is_active", "isActive"):
        if field_name in record and isinstance(record[field_name], bool):
            return "active" if record[field_name] else "disabled"

    raw_status = _first_value(record, ("status", "key_status", "keyStatus", "state"))
    if isinstance(raw_status, bool):
        return "active" if raw_status else "disabled"
    if upstream_type == "newapi" and not isinstance(raw_status, bool):
        numeric_status = _positive_int64(raw_status)
        mapped = {
            1: "active",
            2: "disabled",
            3: "quota_exhausted",
            4: "expired",
        }.get(numeric_status or 0)
        if mapped is not None:
            return mapped
    if not isinstance(raw_status, str):
        return None
    normalized = raw_status.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"active", "enabled", "valid", "normal", "available"}:
        return "active"
    if normalized in {"disabled", "inactive", "blocked", "revoked", "deactivated"}:
        return "disabled"
    if normalized in {"expired", "key_expired"}:
        return "expired"
    if normalized in {"quota_exhausted", "exhausted", "quota_depleted"}:
        return "quota_exhausted"
    return None


def _record_group_identity(record: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    raw_group = record.get("group")
    group_present = "group" in record or any(
        field_name in record
        for field_name in ("group_id", "groupId", "group_name", "groupName")
    )
    if isinstance(raw_group, dict):
        group_id = _clean_identifier(_first_value(raw_group, ("id", "group_id", "groupId")))
        group_name = _clean_text(
            _first_value(raw_group, ("name", "group_name", "groupName")),
            160,
        )
    else:
        group_id = _clean_identifier(_first_value(record, ("group_id", "groupId")))
        group_name = _clean_text(_first_value(record, ("group_name", "groupName")), 160)
        raw_group_text = _clean_text(raw_group, 160) if raw_group is not None else None
        if raw_group_text:
            if raw_group_text.isdigit():
                group_id = group_id or raw_group_text
            else:
                group_name = group_name or raw_group_text
    return group_id, group_name, group_present


def _explicit_group_unavailable(record: dict[str, Any]) -> bool:
    raw_group = record.get("group")
    if isinstance(raw_group, dict):
        raw_status = _first_value(
            raw_group,
            ("status", "group_status", "groupStatus", "state"),
        )
    else:
        raw_status = _first_value(record, ("group_status", "groupStatus"))
    if isinstance(raw_status, bool):
        return not raw_status
    if not isinstance(raw_status, str):
        return False
    normalized = raw_status.strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized in {"disabled", "inactive", "unavailable", "blocked", "expired"}


def _account_upstream_state_from_record(
    upstream_type: str,
    record: dict[str, Any] | None,
    available_groups: _AvailableGroupRefs,
) -> AccountUpstreamState | None:
    if record is None:
        return None
    key_record_id = _api_key_record_id(record)
    key_status = _normalize_api_key_status(upstream_type, record)
    group_id, group_name, group_present = _record_group_identity(record)
    raw_group = record.get("group")
    scalar_group_text = (
        _clean_text(raw_group, 160)
        if raw_group is not None and not isinstance(raw_group, dict)
        else None
    )
    if (
        group_id is None
        and group_name is not None
        and group_name == scalar_group_text
        and group_name.casefold() in available_groups.ids
    ):
        # NewAPI commonly serializes a string group ID in the scalar `group`
        # field. Prefer an authoritative ID match before treating it as a name.
        group_id = group_name
        group_name = None
    if _explicit_group_unavailable(record):
        group_status: str | None = "unavailable"
    elif group_present and group_id is None and group_name is None:
        group_status = "unassigned"
    elif available_groups.authoritative and (group_id is not None or group_name is not None):
        if group_id is not None:
            matches = group_id.casefold() in available_groups.ids
        else:
            matches = bool(
                group_name and group_name.casefold() in available_groups.names
            )
        group_status = "available" if matches else "deleted"
    else:
        group_status = None
    if (
        key_record_id is None
        and key_status is None
        and group_status is None
        and group_id is None
        and group_name is None
    ):
        return None
    return AccountUpstreamState(
        key_record_id=key_record_id,
        key_status=key_status,
        group_status=group_status,
        group_id=group_id,
        group_name=group_name,
    )


def _state_with_usage(
    state: AccountUpstreamState | None,
    usage_amount: Any,
) -> AccountUpstreamState | None:
    amount = _finite_number(usage_amount)
    if amount is None or amount < 0:
        return state
    return replace(
        state or AccountUpstreamState(),
        usage_amount=amount,
        usage_unit="USD",
    )


def _match_account_upstream_states(
    upstream_type: str,
    payloads: dict[str, Any],
    account_api_keys: Mapping[int, str],
    revealed_records: Mapping[str, dict[str, Any]],
    available_groups: _AvailableGroupRefs,
) -> dict[int, AccountUpstreamState]:
    matches: dict[int, AccountUpstreamState] = {}
    for account_id, api_key in account_api_keys.items():
        record = (
            revealed_records.get(api_key)
            or _find_unique_api_key_record(upstream_type, payloads, api_key)
        )
        state = _account_upstream_state_from_record(
            upstream_type,
            record,
            available_groups,
        )
        if state is not None:
            matches[account_id] = state
    return matches


def _match_account_groups(
    upstream_type: str,
    payloads: dict[str, Any],
    groups: list[GroupOption],
    account_api_keys: Mapping[int, str],
    revealed_records: Mapping[str, dict[str, Any]],
    available_groups: _AvailableGroupRefs,
) -> dict[int, AccountGroupMatch]:
    matches: dict[int, AccountGroupMatch] = {}
    for account_id, api_key in account_api_keys.items():
        record = (
            revealed_records.get(api_key)
            or _find_unique_api_key_record(upstream_type, payloads, api_key)
        )
        if record is None:
            continue
        group = _account_group_match_from_record(
            groups,
            record,
            authoritative_groups=available_groups.authoritative,
        )
        if group is not None:
            matches[account_id] = group
    return matches


def _masked_api_key_matches(mask: Any, api_key: str) -> bool:
    if not isinstance(mask, str) or len(mask) > 256:
        return False
    cleaned_mask = mask.strip()
    cleaned_key = _clean_secret(api_key)
    if cleaned_key is None or "*" not in cleaned_mask:
        return False
    if cleaned_mask.startswith("sk-"):
        canonical_mask = cleaned_mask[3:]
        candidate_keys = (cleaned_key[3:] if cleaned_key.startswith("sk-") else cleaned_key,)
        minimum_visible = 4
    elif cleaned_mask.startswith("sk*"):
        canonical_mask = cleaned_mask[2:]
        candidate_keys = (cleaned_key[3:] if cleaned_key.startswith("sk-") else cleaned_key,)
        minimum_visible = 4
    else:
        chunks = re.split(r"\*+", cleaned_mask)
        if len(chunks) != 2 or len(chunks[0]) < 4 or len(chunks[1]) < 4:
            return False
        canonical_mask = cleaned_mask
        candidate_keys = (
            cleaned_key,
            cleaned_key[3:] if cleaned_key.startswith("sk-") else cleaned_key,
        )
        minimum_visible = 8
    if (
        len(re.findall(r"\*+", cleaned_mask)) > 4
        or sum(character != "*" for character in canonical_mask) < minimum_visible
    ):
        return False
    chunks = re.split(r"\*+", canonical_mask)
    pattern = ".+".join(re.escape(chunk) for chunk in chunks)
    return any(re.fullmatch(pattern, candidate_key) is not None for candidate_key in candidate_keys)


def _unique_masked_api_key_records(
    upstream_type: str,
    payloads: dict[str, Any],
    target_keys: set[str],
) -> dict[str, dict[str, Any]]:
    if upstream_type not in {"newapi", "sub2api"} or not target_keys:
        return {}
    records = _deduplicated_api_key_records(upstream_type, payloads)
    candidates_by_key: dict[str, list[int]] = {key: [] for key in target_keys}
    candidate_counts_by_record: dict[int, int] = {}
    for index, record in enumerate(records):
        mask = _first_value(record, ("key", "api_key", "apiKey", "token", "value"))
        for target_key in target_keys:
            if _masked_api_key_matches(mask, target_key):
                candidates_by_key[target_key].append(index)
                candidate_counts_by_record[index] = candidate_counts_by_record.get(index, 0) + 1
    return {
        target_key: records[indexes[0]]
        for target_key, indexes in candidates_by_key.items()
        if len(indexes) == 1 and candidate_counts_by_record.get(indexes[0]) == 1
    }


def _account_group_match_from_record(
    groups: list[GroupOption],
    record: dict[str, Any],
    *,
    authoritative_groups: bool = False,
) -> AccountGroupMatch | None:
    raw_group = record.get("group")
    scalar_group_match: GroupOption | None = None
    if isinstance(raw_group, dict):
        record_id = _clean_identifier(_first_value(raw_group, ("id", "group_id", "groupId")))
        record_name = _clean_text(
            _first_value(raw_group, ("name", "group_name", "groupName")),
            160,
        )
        multiplier_source = raw_group
    else:
        record_id = _clean_identifier(_first_value(record, ("group_id", "groupId")))
        record_name = _clean_text(_first_value(record, ("group_name", "groupName")), 160)
        if raw_group is not None:
            raw_group_text = _clean_text(raw_group, 160)
            if raw_group_text and raw_group_text.isdigit():
                record_id = record_id or raw_group_text
            else:
                if authoritative_groups and raw_group_text:
                    scalar_group_match = _lookup_group(
                        groups,
                        raw_group_text,
                        None,
                    )
                record_name = record_name or raw_group_text
        multiplier_source = record

    selected = scalar_group_match or _lookup_group(groups, record_id, record_name)
    if selected is not None:
        return AccountGroupMatch(
            id=selected.id,
            name=selected.name,
            multiplier=selected.multiplier,
            source=selected.source or "key.group",
        )

    if authoritative_groups:
        # The available-groups endpoint is authoritative. Keep the orphaned
        # identity in the separate health state, but do not expose a synthetic
        # group match that could be used for billing.
        return None

    multiplier, field_name = _first_positive_field(
        multiplier_source,
        ("group_multiplier", "rate_multiplier", "group_ratio", "ratio"),
    )
    # Some Sub2API deployments retain a deleted group_id on an API key even
    # though the group is no longer returned by the available-groups API.  A
    # bare orphaned ID has no authoritative name or rate, so treating the ID as
    # the group name would turn "ungrouped" into a misleading matched group.
    if selected is None and record_name is None and multiplier is None:
        return None
    group_id = record_id or record_name
    group_name = record_name or record_id
    if group_id is None or group_name is None:
        return None
    source_prefix = "key.group" if isinstance(raw_group, dict) else "key"
    return AccountGroupMatch(
        id=group_id,
        name=group_name,
        multiplier=multiplier,
        source=f"{source_prefix}.{field_name}" if field_name else source_prefix,
    )


def _extract_key_records(payload: Any) -> list[dict[str, Any]]:
    current = _unwrap(payload)
    if isinstance(current, list):
        return [item for item in current if isinstance(item, dict)]
    if not isinstance(current, dict):
        return []
    if any(key in current for key in ("api_key", "apiKey", "key", "token")):
        return [current]
    for key in ("items", "records", "list", "tokens", "keys", "api_keys", "apiKeys"):
        child = current.get(key)
        if isinstance(child, (dict, list)):
            return _extract_key_records(child)
    return []


def _select_group(
    groups: list[GroupOption],
    matched_record: dict[str, Any] | None,
    *,
    selected_group_id: str | int | None,
    selected_group_name: str | None,
    available_groups: _AvailableGroupRefs | None = None,
) -> GroupOption | None:
    explicit_id = _clean_identifier(selected_group_id)
    explicit_name = _clean_text(selected_group_name, 160)
    selected = _lookup_group(groups, explicit_id, explicit_name)
    if selected is not None:
        return selected

    if (explicit_id or explicit_name) and available_groups is not None and available_groups.authoritative:
        # An explicitly selected group that is absent from the authoritative
        # list was deleted; do not fall back to an API-key record's stale rate.
        return None

    if matched_record is None:
        return None
    raw_group = matched_record.get("group")
    scalar_group_match: GroupOption | None = None
    if isinstance(raw_group, dict):
        record_id = _clean_identifier(_first_value(raw_group, ("id", "group_id", "groupId")))
        record_name = _clean_text(_first_value(raw_group, ("name", "group_name", "groupName")), 160)
    else:
        record_id = _clean_identifier(_first_value(matched_record, ("group_id", "groupId")))
        record_name = _clean_text(_first_value(matched_record, ("group_name", "groupName")), 160)
        if raw_group is not None:
            raw_group_text = _clean_text(raw_group, 160)
            if raw_group_text and raw_group_text.isdigit():
                record_id = record_id or raw_group_text
            else:
                if (
                    available_groups is not None
                    and available_groups.authoritative
                    and raw_group_text
                ):
                    scalar_group_match = _lookup_group(
                        groups,
                        raw_group_text,
                        None,
                    )
                record_name = record_name or raw_group_text

    selected = scalar_group_match or _lookup_group(groups, record_id, record_name)
    if selected is not None:
        return selected

    if available_groups is not None and available_groups.authoritative:
        return None

    multiplier_source = raw_group if isinstance(raw_group, dict) else matched_record
    record_multiplier, field_name = _first_positive_field(
        multiplier_source,
        ("group_multiplier", "rate_multiplier", "group_ratio", "ratio"),
    )
    if record_multiplier is not None and (record_id or record_name):
        return GroupOption(
            id=record_id or record_name or "",
            name=record_name or record_id or "",
            multiplier=record_multiplier,
            source=f"key.group.{field_name}" if isinstance(raw_group, dict) else f"key.{field_name}",
        )
    return None


def _lookup_group(groups: list[GroupOption], group_id: str | None, group_name: str | None) -> GroupOption | None:
    if group_id is not None:
        return next((group for group in groups if group.id == group_id), None)
    if group_name:
        folded = group_name.casefold()
        return next((group for group in groups if group.name.casefold() == folded), None)
    return None


def _discover_balance(
    upstream_type: str,
    responses: dict[str, _FetchResult],
    *,
    access_token: str | None,
    new_api_user: str | int | None,
) -> _BalanceDiscovery:
    if _clean_secret(access_token) is None:
        return _BalanceDiscovery(
            status="credentials_missing",
            message="An upstream access token is required to read the balance.",
        )

    if upstream_type == "newapi":
        if _clean_new_api_user(new_api_user) is None:
            return _BalanceDiscovery(
                status="credentials_missing",
                message="A numeric New-Api-User ID is required to read the NewAPI balance.",
            )
        return _discover_newapi_balance(responses)
    return _discover_sub2api_balance(responses)


def _discover_newapi_balance(
    responses: dict[str, _FetchResult],
) -> _BalanceDiscovery:
    self_result = responses.get(NEWAPI_BALANCE_ENDPOINT)
    failure = _balance_fetch_failure(self_result, "NewAPI")
    if failure is not None:
        return failure
    assert self_result is not None
    if not _payload_succeeded(self_result.payload):
        return _BalanceDiscovery(
            status="error",
            message="The NewAPI balance response indicated failure.",
        )

    quota_per_unit = DEFAULT_NEWAPI_QUOTA_PER_UNIT
    status_result = responses.get("/api/status")
    if status_result is not None and status_result.ok:
        if not _payload_succeeded(status_result.payload):
            return _BalanceDiscovery(
                status="error",
                message="The NewAPI status response indicated failure.",
            )
        status_data = _unwrap(status_result.payload)
        if isinstance(status_data, dict) and "quota_per_unit" in status_data:
            parsed_quota_per_unit = _positive_number(status_data.get("quota_per_unit"))
            if parsed_quota_per_unit is not None:
                quota_per_unit = parsed_quota_per_unit

    data = _unwrap(self_result.payload)
    if not isinstance(data, dict):
        return _invalid_balance("NewAPI")

    quota = _finite_number(data.get("quota"))
    if quota is None:
        return _invalid_balance("NewAPI")

    used_present, used_quota = _first_finite_field(data, ("used_quota", "quota_used"))
    if used_present and used_quota is None:
        return _invalid_balance("NewAPI")
    total_present, total_quota = _first_finite_field(data, ("total_quota", "quota_total"))
    if total_present and total_quota is None:
        return _invalid_balance("NewAPI")

    remaining = _quota_points_to_usd(quota, quota_per_unit)
    used = (
        _quota_points_to_usd(used_quota, quota_per_unit)
        if used_quota is not None
        else None
    )
    total = (
        _quota_points_to_usd(total_quota, quota_per_unit)
        if total_quota is not None
        else None
    )
    if remaining is None or (used_quota is not None and used is None) or (
        total_quota is not None and total is None
    ):
        return _invalid_balance("NewAPI")

    return _BalanceDiscovery(
        remaining=remaining,
        total=total,
        used=used,
        unit="USD",
        status="ok",
        message="Balance read from the NewAPI user account.",
    )


def _discover_sub2api_balance(
    responses: dict[str, _FetchResult],
) -> _BalanceDiscovery:
    result = responses.get(SUB2API_BALANCE_ENDPOINT)
    failure = _balance_fetch_failure(result, "Sub2API")
    if failure is not None:
        return failure
    assert result is not None
    if not _payload_succeeded(result.payload):
        return _BalanceDiscovery(
            status="error",
            message="The Sub2API balance response indicated failure.",
        )
    data = _unwrap(result.payload)
    if not isinstance(data, dict):
        return _invalid_balance("Sub2API")
    balance = _finite_number(data.get("balance"))
    if balance is None:
        return _invalid_balance("Sub2API")
    return _BalanceDiscovery(
        remaining=balance,
        unit="USD",
        status="ok",
        message="Balance read from the Sub2API user account.",
    )


def _daily_usage_error_detail(
    upstream_type: str,
    responses: dict[str, _FetchResult],
    *,
    period: Literal["today", "yesterday"],
    status: str,
) -> str | None:
    if status == "ok":
        return None
    if upstream_type == "newapi":
        endpoint = (
            NEWAPI_TODAY_USAGE_ENDPOINT
            if period == "today"
            else NEWAPI_YESTERDAY_USAGE_RESPONSE_KEY
        )
    elif upstream_type == "sub2api":
        endpoint = (
            SUB2API_TODAY_USAGE_ENDPOINT
            if period == "today"
            else SUB2API_USAGE_STATS_ENDPOINT
        )
    else:
        return "unsupported_upstream_type"

    result = responses.get(endpoint)
    if result is None:
        return "response_missing"
    if result.error_kind and result.error_kind != "http_status":
        return result.error_kind
    if result.status_code is not None and not result.ok:
        return f"http_{result.status_code}"
    if not _payload_succeeded(result.payload):
        return "upstream_failure"
    if status in {"unsupported", "not_available"}:
        return "field_missing"
    return "invalid_payload"


def _discover_today_balance_usage(
    upstream_type: str,
    responses: dict[str, _FetchResult],
    *,
    access_token: str | None,
    new_api_user: str | int | None,
) -> tuple[float | None, str | None, str]:
    if _clean_secret(access_token) is None:
        return None, None, "credentials_missing"

    if upstream_type == "newapi":
        if _clean_new_api_user(new_api_user) is None:
            return None, None, "credentials_missing"
        return _discover_newapi_period_usage(responses, NEWAPI_TODAY_USAGE_ENDPOINT)

    if upstream_type != "sub2api":
        return None, None, "unsupported"
    return _discover_sub2api_period_usage(
        responses,
        field="today_actual_cost",
        missing_status="error",
    )


def _discover_yesterday_balance_usage(
    upstream_type: str,
    responses: dict[str, _FetchResult],
    *,
    access_token: str | None,
    new_api_user: str | int | None,
) -> tuple[float | None, str | None, str]:
    if _clean_secret(access_token) is None:
        return None, None, "credentials_missing"

    if upstream_type == "newapi":
        if _clean_new_api_user(new_api_user) is None:
            return None, None, "credentials_missing"
        return _discover_newapi_period_usage(
            responses,
            NEWAPI_YESTERDAY_USAGE_RESPONSE_KEY,
        )

    if upstream_type != "sub2api":
        return None, None, "unsupported"
    return _discover_sub2api_yesterday_usage(responses)


def _discover_newapi_period_usage(
    responses: dict[str, _FetchResult],
    response_key: str,
) -> tuple[float | None, str | None, str]:
    result = responses.get(response_key)
    if result is not None and result.status_code in {404, 405}:
        return None, None, "unsupported"
    if result is None or not result.ok or not _payload_succeeded(result.payload):
        return None, None, "error"
    data = _unwrap(result.payload)
    if not isinstance(data, dict):
        return None, None, "error"
    quota = _finite_number(data.get("quota"))
    if quota is None or quota < 0:
        return None, None, "error"

    quota_per_unit = DEFAULT_NEWAPI_QUOTA_PER_UNIT
    status_result = responses.get("/api/status")
    if (
        status_result is not None
        and status_result.ok
        and _payload_succeeded(status_result.payload)
    ):
        status_data = _unwrap(status_result.payload)
        if isinstance(status_data, dict):
            parsed_quota_per_unit = _positive_number(status_data.get("quota_per_unit"))
            if parsed_quota_per_unit is not None:
                quota_per_unit = parsed_quota_per_unit
    amount = _quota_points_to_usd(quota, quota_per_unit)
    if amount is None:
        return None, None, "error"
    return amount, "USD", "ok"


def _discover_sub2api_period_usage(
    responses: dict[str, _FetchResult],
    *,
    field: str,
    missing_status: str,
) -> tuple[float | None, str | None, str]:
    result = responses.get(SUB2API_TODAY_USAGE_ENDPOINT)
    if result is None or not result.ok or not _payload_succeeded(result.payload):
        return None, None, "error"
    data = _unwrap(result.payload)
    if not isinstance(data, dict):
        return None, None, "error"
    if field not in data or data.get(field) is None:
        return None, None, missing_status
    amount = _finite_number(data.get(field))
    if amount is None or amount < 0:
        return None, None, "error"
    return amount, "USD", "ok"


def _discover_sub2api_yesterday_usage(
    responses: dict[str, _FetchResult],
) -> tuple[float | None, str | None, str]:
    result = responses.get(SUB2API_USAGE_STATS_ENDPOINT)
    if result is not None and result.status_code in {404, 405}:
        return None, None, "unsupported"
    if result is None or not result.ok or not _payload_succeeded(result.payload):
        return None, None, "error"
    data = _unwrap(result.payload)
    if not isinstance(data, dict):
        return None, None, "error"
    amount = _finite_number(data.get("total_actual_cost"))
    if amount is None or amount < 0:
        return None, None, "error"
    return amount, "USD", "ok"


_CHANNEL_MONITOR_STATUSES = frozenset(
    {
        "available",
        "degraded",
        "error",
        "failed",
        "healthy",
        "not_checked",
        "operational",
        "timeout",
        "unavailable",
        "unknown",
    }
)
_CHANNEL_MONITOR_STATUS_ALIASES = {
    "active": "available",
    "enabled": "available",
    "ok": "available",
    "success": "available",
}
_CHANNEL_MONITOR_DETAIL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "primary_status": ("primary_status", "primaryStatus", "status"),
    "primary_latency_ms": ("primary_latency_ms", "primaryLatencyMs", "latency_ms"),
    "primary_ping_latency_ms": (
        "primary_ping_latency_ms",
        "primaryPingLatencyMs",
        "ping_latency_ms",
    ),
    "availability_7d": ("availability_7d", "availability7d"),
    "extra_models": ("extra_models", "extraModels"),
    "timeline": ("timeline",),
}
_CHANNEL_MONITOR_DETAIL_CONTAINERS = (
    "monitor",
    "channel_monitor",
    "channelMonitor",
    "result",
    "status",
)


def _discover_channel_monitors(
    upstream_type: str,
    responses: dict[str, _FetchResult],
    *,
    access_token: str | None,
    secrets: Iterable[str | None] = (),
    detail_results: Mapping[int, _FetchResult] | None = None,
) -> tuple[list[dict[str, Any]], int, str, str]:
    if upstream_type == "newapi":
        return _discover_newapi_uptime_monitors(
            responses,
            secrets=secrets,
        )
    if upstream_type != "sub2api":
        return [], 0, "unsupported", "The upstream type does not expose channel monitors."
    if _clean_secret(access_token) is None:
        return [], 0, "credentials_missing", "An upstream access token is required to read channel monitors."

    result = responses.get(SUB2API_CHANNEL_MONITORS_ENDPOINT)
    if result is not None and result.status_code in {404, 405}:
        return [], 0, "unsupported", "The upstream does not expose channel monitors."
    if result is not None and result.status_code in {401, 403}:
        return [], 0, "credentials_rejected", "The upstream rejected channel monitor credentials."
    if result is None or not result.ok:
        return [], 0, "error", "Could not read upstream channel monitors."
    if not _payload_succeeded(result.payload):
        return [], 0, "error", "The upstream channel monitor response indicated failure."

    data = _unwrap(result.payload)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return [], 0, "error", "The upstream returned invalid channel monitor data."

    secret_variants = _secret_variants((*tuple(secrets), access_token))
    monitors: list[dict[str, Any]] = []
    valid_monitor_count = 0
    attempted_detail_ids: set[int] = set()
    successful_detail_ids: set[int] = set()
    details_by_id = detail_results or {}
    for raw in data["items"]:
        if not isinstance(raw, dict):
            continue
        monitor_id = _positive_int64(raw.get("id"))
        if monitor_id is None:
            continue
        valid_monitor_count += 1
        if len(monitors) >= MAX_CHANNEL_MONITORS:
            continue

        merged = dict(raw)
        detail_result = details_by_id.get(monitor_id)
        if detail_result is not None:
            attempted_detail_ids.add(monitor_id)
            detail_patch = _channel_monitor_detail_patch(detail_result)
            if detail_patch:
                merged.update(detail_patch)
                successful_detail_ids.add(monitor_id)
        # Never allow a detail response to replace the validated list ID.
        merged["id"] = monitor_id
        monitor = _sanitize_channel_monitor(
            merged,
            secret_variants=secret_variants,
        )
        if monitor is not None:
            monitors.append(monitor)
    if valid_monitor_count > len(monitors):
        message = (
            f"Read the first {len(monitors)} of {valid_monitor_count} "
            "upstream channel monitors."
        )
    elif monitors:
        message = f"Read {len(monitors)} upstream channel monitor(s)."
    else:
        message = "No upstream channel monitors are available."
    detail_failure_count = len(attempted_detail_ids - successful_detail_ids)
    if detail_failure_count:
        message += (
            f" Used list summaries for {detail_failure_count} monitor(s) whose "
            "status details were unavailable."
        )
    return monitors, valid_monitor_count, "ok", message


def _discover_newapi_uptime_monitors(
    responses: dict[str, _FetchResult],
    *,
    secrets: Iterable[str | None] = (),
) -> tuple[list[dict[str, Any]], int, str, str]:
    result = responses.get(NEWAPI_UPTIME_STATUS_ENDPOINT)
    if result is not None and result.status_code in {404, 405}:
        return [], 0, "unsupported", "The NewAPI upstream does not expose a public uptime panel."
    if result is None or not result.ok:
        return [], 0, "error", "Could not read the NewAPI public uptime panel."
    if not _payload_succeeded(result.payload):
        return [], 0, "error", "The NewAPI uptime response indicated failure."

    data = _unwrap(result.payload)
    if not isinstance(data, list):
        return [], 0, "error", "The NewAPI upstream returned invalid uptime data."
    if not data:
        return [], 0, "not_configured", "The NewAPI upstream has no public uptime monitors configured."

    secret_variants = _secret_variants(tuple(secrets))
    monitors: list[dict[str, Any]] = []
    valid_monitor_count = 0
    incomplete_group_count = 0
    seen_descriptors: set[str] = set()
    monitor_indexes: dict[str, int] = {}
    for raw_group in data:
        if not isinstance(raw_group, dict):
            continue
        category_name = _scrub_text_with_variants(
            _first_value(raw_group, ("categoryName", "category_name", "name")),
            secret_variants,
            160,
        ) or "Uptime"
        raw_monitors = raw_group.get("monitors")
        if not isinstance(raw_monitors, list) or not raw_monitors:
            incomplete_group_count += 1
            continue
        for raw_monitor in raw_monitors:
            if not isinstance(raw_monitor, dict):
                continue
            name = _scrub_text_with_variants(
                raw_monitor.get("name"),
                secret_variants,
                160,
            )
            if name is None:
                continue
            monitor_group = _scrub_text_with_variants(
                raw_monitor.get("group"),
                secret_variants,
                160,
            )
            group_name = " · ".join(
                value for value in (category_name, monitor_group) if value
            )
            descriptor = "\x1f".join((category_name, monitor_group or "", name))
            current_status = _newapi_uptime_status(raw_monitor.get("status"))
            current_availability = _bounded_monitor_number(
                raw_monitor.get("uptime"),
                maximum=100.0,
            )
            if descriptor in seen_descriptors:
                existing_index = monitor_indexes.get(descriptor)
                if existing_index is not None:
                    existing = monitors[existing_index]
                    existing["primary_status"] = _least_healthy_uptime_status(
                        str(existing.get("primary_status") or "unknown"),
                        current_status,
                    )
                    existing_availability = _finite_number(
                        existing.get("availability_7d")
                    )
                    if current_availability is not None and (
                        existing_availability is None
                        or current_availability < existing_availability
                    ):
                        existing["availability_7d"] = current_availability
                continue

            seen_descriptors.add(descriptor)
            valid_monitor_count += 1
            if len(monitors) >= MAX_CHANNEL_MONITORS:
                continue
            # Preserve the original first-occurrence ID while collapsing
            # indistinguishable duplicates into one conservative status.
            monitor_id = _stable_monitor_id(f"{descriptor}\x1f0")
            monitor = _sanitize_channel_monitor(
                {
                    "id": monitor_id,
                    "name": name,
                    "provider": "uptime-kuma",
                    "group_name": group_name,
                    "primary_model": "",
                    "primary_status": current_status,
                    "availability_7d": current_availability,
                    "availability_window": "24h",
                    "extra_models": [],
                    "timeline": [],
                },
                secret_variants=secret_variants,
            )
            if monitor is not None:
                monitor_indexes[descriptor] = len(monitors)
                monitors.append(monitor)

    if incomplete_group_count:
        group_label = "group" if incomplete_group_count == 1 else "groups"
        return (
            monitors,
            valid_monitor_count,
            "error",
            f"The NewAPI public uptime panel returned incomplete data for {incomplete_group_count} configured {group_label}.",
        )
    if not monitors:
        return [], valid_monitor_count, "error", "The NewAPI public uptime panel is configured but returned no monitor data."
    message = f"Read {len(monitors)} NewAPI public uptime monitor(s)."
    if valid_monitor_count > len(monitors):
        message = f"Read the first {len(monitors)} of {valid_monitor_count} NewAPI public uptime monitors."
    return monitors, valid_monitor_count, "ok", message


def _newapi_uptime_status(value: Any) -> str:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return {
        0: "unavailable",
        1: "available",
        2: "degraded",
        3: "degraded",
    }.get(status, "unknown")


def _least_healthy_uptime_status(left: str, right: str) -> str:
    rank = {
        "available": 0,
        "unknown": 1,
        "degraded": 2,
        "unavailable": 3,
    }
    return max((left, right), key=lambda value: rank.get(value, 1))


def _stable_monitor_id(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") & ((1 << 53) - 1)) or 1


def _channel_monitor_detail_patch(result: _FetchResult) -> dict[str, Any]:
    if not result.ok or not _payload_succeeded(result.payload):
        return {}
    data = _unwrap(result.payload)
    if not isinstance(data, dict):
        return {}

    patch: dict[str, Any] = {}
    seen: set[int] = set()

    def merge_candidate(candidate: dict[str, Any], depth: int) -> None:
        candidate_identity = id(candidate)
        if candidate_identity in seen or depth > 3:
            return
        seen.add(candidate_identity)

        for field, aliases in _CHANNEL_MONITOR_DETAIL_FIELD_ALIASES.items():
            for alias in aliases:
                if alias not in candidate:
                    continue
                normalized = _normalize_channel_monitor_detail_field(
                    field,
                    candidate.get(alias),
                )
                if normalized is not None:
                    patch[field] = normalized
                break

        for container in _CHANNEL_MONITOR_DETAIL_CONTAINERS:
            nested = candidate.get(container)
            if isinstance(nested, dict):
                merge_candidate(nested, depth + 1)

    merge_candidate(data, 0)
    return patch


def _normalize_channel_monitor_detail_field(field: str, value: Any) -> Any | None:
    if field == "primary_status":
        status = _normalize_channel_monitor_status(value)
        raw_status = _clean_text(value, 32)
        return (
            status
            if status != "unknown"
            or (raw_status is not None and raw_status.casefold() == "unknown")
            else None
        )
    if field in {"primary_latency_ms", "primary_ping_latency_ms"}:
        return _bounded_monitor_number(value, maximum=86_400_000)
    if field == "availability_7d":
        return _bounded_monitor_number(value, maximum=100)
    if field in {"extra_models", "timeline"}:
        return value if isinstance(value, list) else None
    return None


def _sanitize_channel_monitor(
    raw: Any,
    *,
    secret_variants: Sequence[str] = (),
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    monitor_id = _positive_int64(raw.get("id"))
    if monitor_id is None:
        return None

    extra_models: list[dict[str, Any]] = []
    raw_extra_models = raw.get("extra_models")
    if isinstance(raw_extra_models, list):
        for extra in raw_extra_models:
            if len(extra_models) >= MAX_CHANNEL_MONITOR_EXTRA_MODELS:
                break
            if not isinstance(extra, dict):
                continue
            model = _scrub_text_with_variants(
                _first_value(extra, ("model", "name")),
                secret_variants,
                160,
            )
            if model is None:
                continue
            extra_models.append(
                {
                    "model": model,
                    "status": _normalize_channel_monitor_status(
                        _scrub_text_with_variants(
                            extra.get("status"),
                            secret_variants,
                            32,
                        )
                    ),
                    "latency_ms": _bounded_monitor_number(
                        _first_value(extra, ("latency_ms", "latencyMs")),
                        maximum=86_400_000,
                    ),
                }
            )

    timeline_candidates: list[tuple[datetime, dict[str, Any]]] = []
    raw_timeline = raw.get("timeline")
    if isinstance(raw_timeline, list):
        for point in raw_timeline:
            if not isinstance(point, dict):
                continue
            checked_at = _normalize_monitor_timestamp(
                _scrub_text_with_variants(
                    _first_value(point, ("checked_at", "checkedAt", "time")),
                    secret_variants,
                    64,
                )
            )
            if checked_at is None:
                continue
            parsed_checked_at = _parse_monitor_timestamp(checked_at)
            if parsed_checked_at is None:
                continue
            timeline_candidates.append(
                (
                    parsed_checked_at,
                    {
                    "status": _normalize_channel_monitor_status(
                        _scrub_text_with_variants(
                            point.get("status"),
                            secret_variants,
                            32,
                        )
                    ),
                    "latency_ms": _bounded_monitor_number(
                        _first_value(point, ("latency_ms", "latencyMs")),
                        maximum=86_400_000,
                    ),
                    "ping_latency_ms": _bounded_monitor_number(
                        _first_value(point, ("ping_latency_ms", "pingLatencyMs")),
                        maximum=86_400_000,
                    ),
                    "checked_at": checked_at,
                    },
                )
            )

    timeline = [
        point
        for _, point in sorted(timeline_candidates, key=lambda item: item[0])[
            -MAX_CHANNEL_MONITOR_TIMELINE_POINTS:
        ]
    ]

    return {
        "id": monitor_id,
        "name": _scrub_text_with_variants(
            raw.get("name"),
            secret_variants,
            160,
        )
        or f"Monitor #{monitor_id}",
        "provider": _normalize_channel_monitor_provider(
            _scrub_text_with_variants(
                raw.get("provider"),
                secret_variants,
                64,
            )
        ),
        "group_name": _scrub_text_with_variants(
            raw.get("group_name"),
            secret_variants,
            160,
        )
        or "",
        "primary_model": _scrub_text_with_variants(
            raw.get("primary_model"),
            secret_variants,
            160,
        )
        or "",
        "primary_status": _normalize_channel_monitor_status(
            _scrub_text_with_variants(
                raw.get("primary_status"),
                secret_variants,
                32,
            )
        ),
        "primary_latency_ms": _bounded_monitor_number(
            raw.get("primary_latency_ms"),
            maximum=86_400_000,
        ),
        "primary_ping_latency_ms": _bounded_monitor_number(
            raw.get("primary_ping_latency_ms"),
            maximum=86_400_000,
        ),
        "availability_7d": _bounded_monitor_number(
            raw.get("availability_7d"),
            maximum=100,
        ),
        "availability_window": (
            raw.get("availability_window")
            if raw.get("availability_window") in {"24h", "7d"}
            else "7d"
        ),
        "extra_models": extra_models,
        "timeline": timeline,
    }


def _normalize_channel_monitor_status(value: Any) -> str:
    status = _clean_text(value, 32)
    normalized = status.casefold() if status is not None else ""
    normalized = _CHANNEL_MONITOR_STATUS_ALIASES.get(normalized, normalized)
    return normalized if normalized in _CHANNEL_MONITOR_STATUSES else "unknown"


def _normalize_channel_monitor_provider(value: Any) -> str:
    provider = _clean_text(value, 64)
    if provider is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", provider) is None:
        return "unknown"
    return provider.casefold()


def _bounded_monitor_number(value: Any, *, maximum: float) -> float | None:
    number = _finite_number(value)
    if number is None or number < 0 or number > maximum:
        return None
    return number


def _normalize_monitor_timestamp(value: Any) -> str | None:
    text = _clean_text(value, 64)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.isoformat()
    return normalized[:-6] + "Z" if normalized.endswith("+00:00") else normalized


def _parse_monitor_timestamp(value: Any) -> datetime | None:
    text = _clean_text(value, 64)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _newapi_today_usage_params(
    time_zone: str,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    current, start = _newapi_usage_day(time_zone, now=now)
    return {
        "start_timestamp": int(start.timestamp()),
        "end_timestamp": int(current.timestamp()),
    }


def _newapi_usage_day(
    time_zone: str,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    cleaned_time_zone = _clean_text(time_zone, 80) or DEFAULT_TODAY_TIME_ZONE
    try:
        zone = ZoneInfo(cleaned_time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TODAY_TIME_ZONE)
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    else:
        current = current.astimezone(zone)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return current, start


def _newapi_yesterday_usage_params(
    time_zone: str,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    _, today_start = _newapi_usage_day(time_zone, now=now)
    yesterday_start = today_start - timedelta(days=1)
    return {
        "start_timestamp": int(yesterday_start.timestamp()),
        "end_timestamp": int(today_start.timestamp()) - 1,
    }


def _sub2api_yesterday_usage_params(
    time_zone: str,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    _, today_start = _newapi_usage_day(time_zone, now=now)
    yesterday = today_start.date() - timedelta(days=1)
    date_text = yesterday.isoformat()
    return {
        "start_date": date_text,
        "end_date": date_text,
        "timezone": str(today_start.tzinfo),
    }


def _balance_fetch_failure(
    result: _FetchResult | None,
    upstream_name: str,
) -> _BalanceDiscovery | None:
    if result is not None and result.ok:
        return None
    if result is not None and result.status_code in {401, 403}:
        message = f"{upstream_name} rejected the balance credentials."
    elif result is not None and result.error_kind == "credentials_missing":
        message = f"{upstream_name} balance credentials are missing."
    else:
        message = f"Could not read the {upstream_name} balance."
    return _BalanceDiscovery(status="error", message=message)


def _invalid_balance(upstream_name: str) -> _BalanceDiscovery:
    return _BalanceDiscovery(
        status="error",
        message=f"{upstream_name} returned invalid balance data.",
    )


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _first_finite_field(
    data: dict[str, Any],
    keys: Sequence[str],
) -> tuple[bool, float | None]:
    for key in keys:
        if key in data:
            return True, _finite_number(data.get(key))
    return False, None


def _quota_points_to_usd(value: float, quota_per_unit: float) -> float | None:
    try:
        converted = value / quota_per_unit
    except (OverflowError, ZeroDivisionError):
        return None
    return round(converted, 4) if math.isfinite(converted) else None


_RECHARGE_FIELDS = (
    "balance_recharge_multiplier",
    "recharge_multiplier",
    "recharge_rate_multiplier",
    "recharge_rate",
    "recharge_ratio",
    "pay_multiplier",
)


def _discover_recharge_multiplier(
    upstream_type: str,
    payloads: dict[str, Any],
) -> tuple[float | None, str | None, str]:
    endpoints = (
        (
            "/api/v1/payment/config",
            "/api/v1/payment/checkout-info",
            "/api/status",
        )
        if upstream_type == "newapi"
        else SUB2API_ENDPOINTS
    )
    labels = {
        "/api/pricing": "pricing",
        "/api/status": "status",
        "/api/v1/payment/config": "payment.config",
        "/api/v1/payment/checkout-info": "payment.checkout-info",
    }
    usable_recharge_endpoint = False
    for endpoint in endpoints:
        if endpoint not in payloads or endpoint not in labels:
            continue
        usable_recharge_endpoint = True
        payload = _unwrap(payloads[endpoint])
        for field_name in _RECHARGE_FIELDS:
            state, value = _find_positive_key_state(payload, field_name)
            if state == "invalid":
                raise ValueError("invalid recharge multiplier")
            if state == "valid" and value is not None:
                cost_per_usd = _credit_per_cny_to_cost_per_usd(value)
                if cost_per_usd is None:
                    raise ValueError("invalid recharge multiplier")
                return cost_per_usd, f"{labels[endpoint]}.{field_name}", "ok"
        if upstream_type == "newapi" and endpoint == "/api/status":
            state, status_price = _find_positive_key_state(payload, "price")
            if state == "invalid":
                raise ValueError("invalid recharge multiplier")
            if state == "valid" and status_price is not None:
                # upstream-ops stores 1 / price and then applies it in divide
                # mode. Our normalized field stores the resulting CNY/USD
                # cost directly, which is exactly NewAPI's status.price.
                return status_price, "status.price", "ok"
    if usable_recharge_endpoint:
        return None, None, "missing"
    return None, None, "error"


def _credit_per_cny_to_cost_per_usd(value: float) -> float | None:
    """Convert upstream USD credited per CNY into CNY paid per USD."""

    try:
        return _positive_number(1.0 / value)
    except (OverflowError, ZeroDivisionError):
        return None


def _find_positive_key_state(
    payload: Any,
    key: str,
    *,
    depth: int = 0,
) -> tuple[str, float | None]:
    if depth > 8:
        return "missing", None
    found_invalid = False
    if isinstance(payload, dict):
        if key in payload:
            value = _positive_number(payload.get(key))
            if value is not None:
                return "valid", value
            found_invalid = True
        for child in payload.values():
            if not isinstance(child, (dict, list)):
                continue
            state, value = _find_positive_key_state(child, key, depth=depth + 1)
            if state == "valid":
                return state, value
            if state == "invalid":
                found_invalid = True
    elif isinstance(payload, list):
        for child in payload:
            if not isinstance(child, (dict, list)):
                continue
            state, value = _find_positive_key_state(child, key, depth=depth + 1)
            if state == "valid":
                return state, value
            if state == "invalid":
                found_invalid = True
    return ("invalid", None) if found_invalid else ("missing", None)


def _find_positive_key(payload: Any, key: str, *, depth: int = 0) -> float | None:
    if depth > 8:
        return None
    if isinstance(payload, dict):
        if key in payload:
            value = _positive_number(payload.get(key))
            if value is not None:
                return value
        for child in payload.values():
            if isinstance(child, (dict, list)):
                value = _find_positive_key(child, key, depth=depth + 1)
                if value is not None:
                    return value
    elif isinstance(payload, list):
        for child in payload:
            if isinstance(child, (dict, list)):
                value = _find_positive_key(child, key, depth=depth + 1)
                if value is not None:
                    return value
    return None


def _first_positive_field(data: dict[str, Any], keys: Sequence[str]) -> tuple[float | None, str | None]:
    for key in keys:
        value = _positive_number(data.get(key))
        if value is not None:
            return value, key
    return None, None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _first_value(data: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _clean_identifier(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = _clean_text(value, 160)
    return text or None


def _positive_int64(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if 0 < parsed <= 9_223_372_036_854_775_807 else None


def _clean_text(value: Any, maximum: int) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = " ".join(str(value).strip().split())
    text = "".join(char for char in text if char.isprintable())
    return text[:maximum] or None


def _content_length(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _success_message(group_multiplier: float | None, recharge_multiplier: float | None) -> str:
    if group_multiplier is not None and recharge_multiplier is not None:
        return "Upstream group and recharge multipliers discovered."
    if group_multiplier is not None:
        return "Upstream group multiplier discovered; recharge multiplier was not available."
    if recharge_multiplier is not None:
        return "Upstream recharge multiplier discovered; no group was matched."
    return "Connected to upstream service; no usable multiplier was discovered."


def _discovery_failure(
    upstream_type: str,
    source: str,
    endpoints: Sequence[str],
    responses: dict[str, _FetchResult],
) -> DiscoveryResult:
    auth_result = responses.get(SUB2API_BALANCE_ENDPOINT)
    sub2api_auth_rejected = (
        SUB2API_BALANCE_ENDPOINT in endpoints
        and auth_result is not None
        and auth_result.status_code == 401
    )
    for endpoint in endpoints:
        result = responses.get(endpoint)
        if result is not None and result.status_code is not None and result.error_kind == "http_status":
            return replace(
                _error_result(
                    upstream_type,
                    source,
                    f"Upstream request returned HTTP {result.status_code}.",
                ),
                sub2api_auth_rejected=sub2api_auth_rejected,
            )
    if upstream_type == "auto" and any(result.ok for result in responses.values()):
        return replace(
            _error_result(upstream_type, source, "Could not identify the upstream API type."),
            sub2api_auth_rejected=sub2api_auth_rejected,
        )
    return replace(
        _error_result(upstream_type, source, "Could not read a valid upstream response."),
        sub2api_auth_rejected=sub2api_auth_rejected,
    )


def _error_result(upstream_type: str, source: str, message: str) -> DiscoveryResult:
    return DiscoveryResult(
        upstream_type=upstream_type,
        source=source,
        status="error",
        message=message,
    )


__all__ = [
    "AccountGroupMatch",
    "AccountUpstreamState",
    "DEFAULT_TODAY_TIME_ZONE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DiscoveryResult",
    "GroupOption",
    "MAX_RESPONSE_BYTES",
    "MAX_UPSTREAM_TOKEN_LENGTH",
    "NEWAPI_ENDPOINTS",
    "NEWAPI_TODAY_USAGE_ENDPOINT",
    "NEWAPI_UPTIME_STATUS_ENDPOINT",
    "SUB2API_API_KEY_USAGE_ENDPOINT",
    "SUB2API_CHANNEL_MONITORS_ENDPOINT",
    "SUB2API_ENDPOINTS",
    "SUB2API_REFRESH_ENDPOINT",
    "SUB2API_TODAY_USAGE_ENDPOINT",
    "SUB2API_USAGE_STATS_ENDPOINT",
    "UPSTREAM_USAGE_TIMEOUT_SECONDS",
    "Sub2ApiTokenPair",
    "UpstreamClient",
    "UpstreamDiscoveryClient",
    "discover_upstream",
    "refresh_sub2api_tokens",
]
