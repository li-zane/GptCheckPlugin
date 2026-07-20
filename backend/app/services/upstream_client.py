from __future__ import annotations

import asyncio
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
from datetime import datetime, timedelta
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
DEFAULT_NEWAPI_QUOTA_PER_UNIT = 500_000.0
DEFAULT_TODAY_TIME_ZONE = "Asia/Shanghai"
USAGE_STATS_TIMEOUT_SECONDS = 30.0
# Upstream key-list requests use page_size=200. Keep detail discovery capable
# of covering the complete returned page while bounding the request fan-out.
MAX_AUTOMATIC_KEY_REVEALS = 200
KEY_REVEAL_CONCURRENCY = 20
SUB2API_API_KEY_USAGE_BATCH_SIZE = 100
NEWAPI_BALANCE_ENDPOINT = "/api/user/self"
NEWAPI_TODAY_USAGE_ENDPOINT = "/api/log/self/stat"
NEWAPI_YESTERDAY_USAGE_RESPONSE_KEY = "newapi:yesterday-usage"
SUB2API_BALANCE_ENDPOINT = "/api/v1/auth/me"
SUB2API_TODAY_USAGE_ENDPOINT = "/api/v1/usage/dashboard/stats"
SUB2API_USAGE_STATS_ENDPOINT = "/api/v1/usage/stats"
SUB2API_API_KEY_USAGE_ENDPOINT = "/api/v1/usage/dashboard/api-keys-usage"
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
)

NEWAPI_PRIMARY_ENDPOINTS: tuple[str, ...] = (
    "/api/user/self/groups",
    "/api/pricing",
    "/api/user/self",
    NEWAPI_TODAY_USAGE_ENDPOINT,
    "/api/token/?p=1&page_size=200",
    "/api/v1/payment/checkout-info",
    "/api/status",
)
SUB2API_PRIMARY_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/groups/available",
    "/api/v1/groups/rates",
    "/api/v1/keys?page=1&page_size=200",
    "/api/v1/payment/checkout-info",
    SUB2API_BALANCE_ENDPOINT,
    SUB2API_TODAY_USAGE_ENDPOINT,
    SUB2API_USAGE_STATS_ENDPOINT,
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
        optimized_endpoint_fallbacks: bool = False,
        today_timezone: str = DEFAULT_TODAY_TIME_ZONE,
    ) -> DiscoveryResult:
        raw_account_api_keys = account_api_keys if isinstance(account_api_keys, Mapping) else {}
        secrets = (api_key, access_token, *raw_account_api_keys.values())
        normalized_account_api_keys = _normalize_account_api_keys(raw_account_api_keys)
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

        endpoints = (
            _ordered_union(NEWAPI_ENDPOINTS, SUB2API_ENDPOINTS)
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
        newapi_today_usage_params = _newapi_today_usage_params(today_timezone)
        newapi_yesterday_usage_params = _newapi_yesterday_usage_params(today_timezone)

        timeout = httpx.Timeout(self.timeout_seconds)
        fetched: list[_FetchResult] = []
        newapi_yesterday_usage_result: _FetchResult | None = None
        revealed_api_key_records: dict[str, dict[str, Any]] = {}
        sub2api_api_key_usage_by_key: dict[str, float] = {}
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
                        result = await self._request_json(
                            client,
                            normalized_url,
                            endpoint,
                            headers=endpoint_headers,
                            params=endpoint_params,
                            timeout_seconds=(
                                max(self.timeout_seconds, USAGE_STATS_TIMEOUT_SECONDS)
                                if endpoint
                                in {
                                    NEWAPI_TODAY_USAGE_ENDPOINT,
                                    SUB2API_TODAY_USAGE_ENDPOINT,
                                    SUB2API_USAGE_STATS_ENDPOINT,
                                }
                                else None
                            ),
                        )
                        return result

                    fetched = await asyncio.gather(
                        *(fetch_endpoint(endpoint) for endpoint in endpoints)
                    )
                    if optimized_endpoint_fallbacks and requested_type in {"newapi", "sub2api"}:
                        primary_responses = dict(zip(endpoints, fetched))
                        compatibility_endpoints = _missing_compatibility_endpoints(
                            requested_type,
                            primary_responses,
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
                    if candidate_type == "newapi":
                        yesterday_headers = _headers_for_endpoint(
                            NEWAPI_TODAY_USAGE_ENDPOINT,
                            requested_type=requested_type,
                            access_token=access_token,
                            api_key=api_key,
                            new_api_user=new_api_user,
                        )
                        newapi_yesterday_usage_result = (
                            _FetchResult(ok=False, error_kind="credentials_missing")
                            if yesterday_headers is None
                            else await self._request_json(
                                client,
                                normalized_url,
                                NEWAPI_TODAY_USAGE_ENDPOINT,
                                headers=yesterday_headers,
                                params=newapi_yesterday_usage_params,
                                timeout_seconds=max(
                                    self.timeout_seconds,
                                    USAGE_STATS_TIMEOUT_SECONDS,
                                ),
                            )
                        )
                    if candidate_type in {"newapi", "sub2api"} and target_api_keys:
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
                        try:
                            revealed_api_key_records = await self._reveal_api_key_records(
                                client,
                                normalized_url,
                                upstream_type=candidate_type,
                                payloads=candidate_payloads,
                                target_keys=target_api_keys,
                                access_token=access_token,
                                new_api_user=new_api_user,
                            )
                        except Exception:
                            # Group and balance discovery remain useful when a
                            # provider does not support automatic key reveal.
                            revealed_api_key_records = {}
                        if candidate_type == "sub2api":
                            try:
                                sub2api_api_key_usage_by_key = (
                                    await self._fetch_sub2api_api_key_usage(
                                        client,
                                        normalized_url,
                                        payloads=candidate_payloads,
                                        target_keys=target_api_keys,
                                        revealed_records=revealed_api_key_records,
                                        access_token=access_token,
                                    )
                                )
                            except Exception:
                                # Key state and group discovery remain useful
                                # when daily per-key statistics are unavailable.
                                sub2api_api_key_usage_by_key = {}
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
                return safe(_discovery_failure(requested_type, "auto", endpoints, responses))
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
        if not usable:
            return safe(_discovery_failure(active_type, source, active_endpoints, responses))

        try:
            groups = _discover_groups(active_type, usable)
            masked_direct_records = _unique_masked_api_key_records(
                active_type,
                usable,
                {normalized_api_key} if normalized_api_key is not None else set(),
            )
            matched_record = (
                revealed_api_key_records.get(normalized_api_key or "")
                or _find_unique_api_key_record(active_type, usable, normalized_api_key)
                or masked_direct_records.get(normalized_api_key or "")
            )
            available_group_refs = _available_group_refs(active_type, usable)
            matched_group = _select_group(
                groups,
                matched_record,
                selected_group_id=selected_group_id,
                selected_group_name=selected_group_name,
            )
            account_group_matches = _match_account_groups(
                active_type,
                usable,
                groups,
                normalized_account_api_keys,
                revealed_api_key_records,
            )
            matched_account_state = _account_upstream_state_from_record(
                active_type,
                matched_record,
                available_group_refs,
                usage_amount=sub2api_api_key_usage_by_key.get(
                    normalized_api_key or ""
                ),
            )
            account_upstream_states = _match_account_upstream_states(
                active_type,
                usable,
                normalized_account_api_keys,
                revealed_api_key_records,
                available_group_refs,
                usage_by_api_key=sub2api_api_key_usage_by_key,
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

        masked_records = _unique_masked_api_key_records(
            upstream_type,
            payloads,
            target_keys,
        )
        pending_keys = {
            key
            for key in target_keys
            if _find_unique_api_key_record(upstream_type, payloads, key) is None
            and key not in masked_records
        }
        if not pending_keys:
            return {}

        records_by_id: dict[int, dict[str, Any]] = {}
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
                records_by_id.setdefault(record_id, record)
            if len(records_by_id) >= MAX_AUTOMATIC_KEY_REVEALS:
                break
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
            revealed_key = _clean_secret(
                _first_value(data, ("key", "api_key", "apiKey", "token", "value"))
            )
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

    async def _fetch_sub2api_api_key_usage(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        payloads: dict[str, Any],
        target_keys: set[str],
        revealed_records: Mapping[str, dict[str, Any]],
        access_token: str | None,
    ) -> dict[str, float]:
        token = _clean_secret(access_token)
        if token is None or not target_keys:
            return {}

        matched_records = _matched_target_api_key_records(
            "sub2api",
            payloads,
            target_keys,
            revealed_records,
        )
        key_by_record_id: dict[int, str] = {}
        for api_key, record in matched_records.items():
            record_id = _positive_int64(
                _first_value(
                    record,
                    ("id", "api_key_id", "apiKeyId", "key_id", "keyId"),
                )
            )
            if record_id is not None:
                key_by_record_id.setdefault(record_id, api_key)
        record_ids = sorted(key_by_record_id)
        if not record_ids:
            return {}

        headers = _build_headers(
            access_token=token,
            api_key=None,
            new_api_user=None,
        )
        usage_by_record_id: dict[int, float] = {}
        for offset in range(0, len(record_ids), SUB2API_API_KEY_USAGE_BATCH_SIZE):
            batch = record_ids[offset : offset + SUB2API_API_KEY_USAGE_BATCH_SIZE]
            result = await self._request_json(
                client,
                base_url,
                SUB2API_API_KEY_USAGE_ENDPOINT,
                method="POST",
                headers=headers,
                json_body={"api_key_ids": batch},
                timeout_seconds=max(
                    self.timeout_seconds,
                    USAGE_STATS_TIMEOUT_SECONDS,
                ),
            )
            usage_by_record_id.update(
                _parse_sub2api_api_key_usage_batch(result, expected_ids=set(batch))
            )
        return {
            key_by_record_id[record_id]: amount
            for record_id, amount in usage_by_record_id.items()
            if record_id in key_by_record_id
        }

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
        try:
            async with client.stream(
                method,
                f"{base_url}{endpoint}",
                headers=headers,
                params=params,
                json=json_body,
                timeout=(
                    httpx.Timeout(timeout_seconds)
                    if timeout_seconds is not None
                    else client.timeout
                ),
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
    optimized_endpoint_fallbacks: bool = False,
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
        optimized_endpoint_fallbacks=optimized_endpoint_fallbacks,
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
    # Status is a public NewAPI endpoint. Do not send either stored credential
    # to it even though every request remains pinned to the validated origin.
    if endpoint == "/api/status":
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
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value)
    for secret in _secret_variants(secrets):
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


def _scrub_discovery_result(
    result: DiscoveryResult,
    secrets: Iterable[str | None],
) -> DiscoveryResult:
    scrubbed_groups = [_scrub_group_option(group, secrets) for group in result.groups]
    scrubbed_match = (
        _scrub_group_option(result.matched_group, secrets)
        if result.matched_group is not None
        else None
    )
    scrubbed_account_matches = {
        account_id: _scrub_account_group_match(group, secrets)
        for account_id, group in result.account_group_matches.items()
    }
    scrubbed_account_state = (
        _scrub_account_upstream_state(result.matched_account_state, secrets)
        if result.matched_account_state is not None
        else None
    )
    scrubbed_account_states = {
        account_id: _scrub_account_upstream_state(state, secrets)
        for account_id, state in result.account_upstream_states.items()
    }
    return replace(
        result,
        groups=scrubbed_groups,
        matched_group=scrubbed_match,
        account_group_matches=scrubbed_account_matches,
        matched_account_state=scrubbed_account_state,
        account_upstream_states=scrubbed_account_states,
        discovered_group_multiplier_source=_scrub_text(
            result.discovered_group_multiplier_source,
            secrets,
            160,
        ),
        discovered_recharge_multiplier_source=_scrub_text(
            result.discovered_recharge_multiplier_source,
            secrets,
            160,
        ),
        balance_message=_scrub_text(result.balance_message, secrets, 300) or "",
        today_balance_error=_scrub_text(result.today_balance_error, secrets, 80),
        yesterday_balance_error=_scrub_text(result.yesterday_balance_error, secrets, 80),
        message=_scrub_text(result.message, secrets, 500) or "Upstream discovery completed.",
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
                if (candidate.id is not None and current.id == candidate.id)
                or (candidate.name and current.name.casefold() == candidate.name.casefold())
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
            name_matches = name is not None and group.name.casefold() == name.casefold()
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
    masked_records = _unique_masked_api_key_records(
        upstream_type,
        payloads,
        target_keys,
    )
    matched: dict[str, dict[str, Any]] = {}
    for target_key in target_keys:
        record = (
            revealed_records.get(target_key)
            or _find_unique_api_key_record(upstream_type, payloads, target_key)
            or masked_records.get(target_key)
        )
        if record is not None:
            matched[target_key] = record
    return matched


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
    endpoints = (
        (
            "/api/token/?p=1&page_size=200",
            "/api/token/search?p=1&size=200",
            "/api/v1/keys?page=1&page_size=200",
        )
        if upstream_type == "newapi"
        else (
            "/api/v1/keys?page=1&page_size=200",
            "/api/v1/api-keys?page=1&page_size=200",
        )
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
    *,
    usage_amount: float | None = None,
) -> AccountUpstreamState | None:
    if record is None:
        return None
    key_status = _normalize_api_key_status(upstream_type, record)
    group_id, group_name, group_present = _record_group_identity(record)
    if _explicit_group_unavailable(record):
        group_status: str | None = "unavailable"
    elif group_present and group_id is None and group_name is None:
        group_status = "unassigned"
    elif available_groups.authoritative and (group_id is not None or group_name is not None):
        matches = bool(
            (group_id and group_id.casefold() in available_groups.ids)
            or (group_name and group_name.casefold() in available_groups.names)
        )
        group_status = "available" if matches else "unavailable"
    else:
        group_status = None
    if (
        key_status is None
        and group_status is None
        and group_id is None
        and group_name is None
        and usage_amount is None
    ):
        return None
    return AccountUpstreamState(
        key_status=key_status,
        group_status=group_status,
        group_id=group_id,
        group_name=group_name,
        usage_amount=usage_amount,
        usage_unit="USD" if usage_amount is not None else None,
    )


def _match_account_upstream_states(
    upstream_type: str,
    payloads: dict[str, Any],
    account_api_keys: Mapping[int, str],
    revealed_records: Mapping[str, dict[str, Any]],
    available_groups: _AvailableGroupRefs,
    *,
    usage_by_api_key: Mapping[str, float] | None = None,
) -> dict[int, AccountUpstreamState]:
    matches: dict[int, AccountUpstreamState] = {}
    usage_by_api_key = usage_by_api_key or {}
    masked_records = _unique_masked_api_key_records(
        upstream_type,
        payloads,
        set(account_api_keys.values()),
    )
    for account_id, api_key in account_api_keys.items():
        record = (
            revealed_records.get(api_key)
            or _find_unique_api_key_record(upstream_type, payloads, api_key)
            or masked_records.get(api_key)
        )
        state = _account_upstream_state_from_record(
            upstream_type,
            record,
            available_groups,
            usage_amount=usage_by_api_key.get(api_key),
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
) -> dict[int, AccountGroupMatch]:
    matches: dict[int, AccountGroupMatch] = {}
    masked_records = _unique_masked_api_key_records(
        upstream_type,
        payloads,
        set(account_api_keys.values()),
    )
    for account_id, api_key in account_api_keys.items():
        record = (
            revealed_records.get(api_key)
            or _find_unique_api_key_record(upstream_type, payloads, api_key)
            or masked_records.get(api_key)
        )
        if record is None:
            continue
        group = _account_group_match_from_record(groups, record)
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
) -> AccountGroupMatch | None:
    raw_group = record.get("group")
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
                record_name = record_name or raw_group_text
        multiplier_source = record

    selected = _lookup_group(groups, record_id, record_name)
    if selected is not None:
        return AccountGroupMatch(
            id=selected.id,
            name=selected.name,
            multiplier=selected.multiplier,
            source=selected.source or "key.group",
        )

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
) -> GroupOption | None:
    explicit_id = _clean_identifier(selected_group_id)
    explicit_name = _clean_text(selected_group_name, 160)
    selected = _lookup_group(groups, explicit_id, explicit_name)
    if selected is not None:
        return selected

    if matched_record is None:
        return None
    raw_group = matched_record.get("group")
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
                record_name = record_name or raw_group_text

    selected = _lookup_group(groups, record_id, record_name)
    if selected is not None:
        return selected

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
        match = next((group for group in groups if group.id == group_id), None)
        if match is not None:
            return match
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


def _parse_sub2api_api_key_usage_batch(
    result: _FetchResult,
    *,
    expected_ids: set[int],
) -> dict[int, float]:
    if (
        not expected_ids
        or not result.ok
        or not _payload_succeeded(result.payload)
    ):
        return {}
    data = _unwrap(result.payload)
    if isinstance(data, dict):
        for key in ("stats", "results", "items"):
            if isinstance(data.get(key), (dict, list)):
                data = data[key]
                break
    records = (
        list(data.items())
        if isinstance(data, dict)
        else [(None, item) for item in data]
        if isinstance(data, list)
        else []
    )
    parsed: dict[int, float] = {}
    for keyed_id, raw in records:
        if not isinstance(raw, dict):
            continue
        record_id = _positive_int64(
            _first_value(
                raw,
                ("api_key_id", "apiKeyId", "key_id", "keyId", "id"),
            )
            or keyed_id
        )
        amount = _finite_number(
            _first_value(
                raw,
                ("today_actual_cost", "todayActualCost", "actual_cost", "cost"),
            )
        )
        if record_id in expected_ids and amount is not None and amount >= 0:
            parsed[record_id] = amount
    return parsed


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
    "SUB2API_ENDPOINTS",
    "SUB2API_REFRESH_ENDPOINT",
    "SUB2API_TODAY_USAGE_ENDPOINT",
    "Sub2ApiTokenPair",
    "UpstreamClient",
    "UpstreamDiscoveryClient",
    "discover_upstream",
    "refresh_sub2api_tokens",
]
