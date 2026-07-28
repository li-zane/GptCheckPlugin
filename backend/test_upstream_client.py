from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from app.services.upstream_client import (
    CHANNEL_MONITOR_DETAIL_CONCURRENCY,
    DiscoveryResult,
    MAX_DOH_RESPONSE_BYTES,
    MAX_CHANNEL_MONITORS,
    MAX_CHANNEL_MONITOR_EXTRA_MODELS,
    MAX_CHANNEL_MONITOR_TIMELINE_POINTS,
    MAX_RESPONSE_BYTES,
    MAX_UPSTREAM_TOKEN_LENGTH,
    NEWAPI_ENDPOINTS,
    NEWAPI_TODAY_USAGE_ENDPOINT,
    NEWAPI_UPTIME_STATUS_ENDPOINT,
    SUB2API_API_KEY_USAGE_ENDPOINT,
    SUB2API_CHANNEL_MONITORS_ENDPOINT,
    SUB2API_REFRESH_ENDPOINT,
    SUB2API_TODAY_USAGE_ENDPOINT,
    SUB2API_USAGE_STATS_ENDPOINT,
    UPSTREAM_USAGE_TIMEOUT_SECONDS,
    UpstreamClient,
    _default_resolver,
    _doh_resolver,
    _invalidate_dns_cache,
    _newapi_today_usage_params,
    _newapi_yesterday_usage_params,
    _scrub_discovery_result,
    _sub2api_yesterday_usage_params,
    _unique_masked_api_key_records,
    discover_upstream,
)


PUBLIC_ADDRESS = "93.184.216.34"
SECOND_PUBLIC_ADDRESS = "1.1.1.1"


def public_resolver(_hostname: str) -> list[str]:
    return [PUBLIC_ADDRESS]


def request_target(request: httpx.Request) -> str:
    query = request.url.query.decode("ascii")
    return f"{request.url.path}?{query}" if query else request.url.path


class UpstreamClientTests(unittest.TestCase):
    def run_discovery(self, handler, **kwargs):
        client = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=public_resolver,
        )
        return asyncio.run(client.discover("https://upstream.example", **kwargs))

    def run_discovery_wrapper(self, handler, **kwargs):
        return asyncio.run(
            discover_upstream(
                "https://upstream.example",
                transport=httpx.MockTransport(handler),
                resolver=public_resolver,
                **kwargs,
            )
        )

    def run_refresh(self, handler, *, base_url: str = "https://upstream.example"):
        client = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=public_resolver,
        )
        return asyncio.run(client.refresh_sub2api_tokens(base_url, "rt-old-private"))

    def test_optimized_discovery_only_uses_compatibility_endpoints_when_primary_data_is_missing(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            seen.append(target)
            payloads = {
                "/api/user/self/groups": {
                    "success": True,
                    "data": [{"id": "default", "name": "Default", "ratio": 1}],
                },
                "/api/pricing": {"success": True, "data": []},
                "/api/user/self": {
                    "success": True,
                    "data": {"quota": 500000, "used_quota": 0},
                },
                "/api/token/?p=1&page_size=200": {
                    "success": True,
                    "data": [{"id": 1, "key": "sk-primary", "group": "default", "status": 1}],
                },
                "/api/v1/payment/checkout-info": {
                    "code": 0,
                    "data": {"balance_recharge_multiplier": 10},
                },
                "/api/status": {"success": True, "data": {"price": 0.1}},
            }
            return httpx.Response(200, json=payloads.get(target, {"success": False}))

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="admin-token",
            new_api_user="7",
            account_api_keys={7: "sk-primary"},
            optimized_endpoint_fallbacks=True,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(seen), 9)
        self.assertNotIn("/api/token/search?p=1&size=200", seen)
        self.assertNotIn("/api/v1/payment/config", seen)
        self.assertEqual(seen.count("/api/status"), 1)
        self.assertEqual(seen.count(NEWAPI_UPTIME_STATUS_ENDPOINT), 1)

    def test_sub2api_refresh_posts_expected_contract_and_parses_envelope(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "access_token": "at-rotated-private",
                        "refresh_token": "rt-rotated-private",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                },
            )

        pair = self.run_refresh(handler, base_url="https://upstream.example/api/v1/")

        self.assertIsNotNone(pair)
        assert pair is not None
        self.assertEqual(pair.access_token, "at-rotated-private")
        self.assertEqual(pair.refresh_token, "rt-rotated-private")
        self.assertEqual(pair.expires_in, 3600)
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request_target(request), SUB2API_REFRESH_ENDPOINT)
        self.assertEqual(json.loads(request.content), {"refresh_token": "rt-old-private"})
        self.assertTrue(request.headers.get("Content-Type", "").startswith("application/json"))
        self.assertIsNone(request.headers.get("Authorization"))
        self.assertIsNone(request.headers.get("New-Api-User"))

    def test_sub2api_refresh_rejects_non_https_before_transport(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        result = self.run_refresh(handler, base_url="http://upstream.example")

        self.assertIsNone(result)
        self.assertEqual(calls, 0)

    def test_sub2api_refresh_rejects_failures_and_incomplete_token_pairs(self) -> None:
        complete_pair = {
            "access_token": "at-rotated-private",
            "refresh_token": "rt-rotated-private",
        }
        cases = (
            (401, {"code": 401, "message": "invalid refresh token"}),
            (200, {"code": 401, "data": complete_pair}),
            (200, {"code": 0, "data": {"access_token": "at-only"}}),
            (200, {"code": 0, "data": {"refresh_token": "rt-only"}}),
            (
                200,
                {
                    "code": 0,
                    "data": {
                        "access_token": "at-valid",
                        "refresh_token": "r" * (MAX_UPSTREAM_TOKEN_LENGTH + 1),
                    },
                },
            ),
        )
        for status_code, payload in cases:
            with self.subTest(status_code=status_code, payload_keys=sorted(payload)):
                result = self.run_refresh(
                    lambda _request, status_code=status_code, payload=payload: httpx.Response(
                        status_code,
                        json=payload,
                    )
                )
                self.assertIsNone(result)

        invalid_json = self.run_refresh(
            lambda _request: httpx.Response(200, content=b"not-json")
        )
        self.assertIsNone(invalid_json)

    def test_default_resolver_replaces_only_proxy_fake_ip_answers_with_doh(self) -> None:
        fake_info = [(None, None, None, None, ("198.18.0.79", 0))]
        with (
            patch("app.services.upstream_client.socket.getaddrinfo", return_value=fake_info),
            patch(
                "app.services.upstream_client._doh_resolver",
                new=AsyncMock(return_value=[PUBLIC_ADDRESS]),
            ) as doh,
        ):
            result = asyncio.run(_default_resolver("upstream.example"))

        self.assertEqual(result, [PUBLIC_ADDRESS])
        doh.assert_awaited_once_with("upstream.example")

    def test_default_resolver_does_not_bypass_other_private_dns_answers(self) -> None:
        private_info = [(None, None, None, None, ("10.0.0.2", 0))]
        with (
            patch("app.services.upstream_client.socket.getaddrinfo", return_value=private_info),
            patch("app.services.upstream_client._doh_resolver", new=AsyncMock()) as doh,
        ):
            result = asyncio.run(_default_resolver("internal.example"))

        self.assertEqual(result, ["10.0.0.2"])
        doh.assert_not_awaited()

    def test_doh_response_limit_stops_consuming_oversized_stream(self) -> None:
        class GuardedOversizedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * (MAX_DOH_RESPONSE_BYTES + 1)
                raise AssertionError("DoH response was consumed after exceeding its limit")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=GuardedOversizedStream())

        real_async_client = httpx.AsyncClient

        def bounded_client(**kwargs):
            return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch("app.services.upstream_client.httpx.AsyncClient", side_effect=bounded_client):
            result = asyncio.run(_doh_resolver("upstream.example"))

        self.assertEqual(result, [])

    def test_doh_resolves_ipv4_and_ipv6_in_parallel(self) -> None:
        active = 0
        peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            record_type = request.url.params.get("type")
            address = "93.184.216.34" if record_type == "A" else "2606:2800:220:1:248:1893:25c8:1946"
            return httpx.Response(
                200,
                json={"Status": 0, "Answer": [{"data": address}]},
            )

        real_async_client = httpx.AsyncClient

        def bounded_client(**kwargs):
            return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch("app.services.upstream_client.httpx.AsyncClient", side_effect=bounded_client):
            result = asyncio.run(_doh_resolver("parallel-dns.example"))

        self.assertEqual(peak, 2)
        self.assertEqual(
            result,
            ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
        )

    def test_default_dns_results_are_cached_across_discovery_clients(self) -> None:
        hostname = "verified-address-cache.example"
        calls = 0

        async def resolver(_hostname: str) -> list[str]:
            nonlocal calls
            calls += 1
            return [PUBLIC_ADDRESS]

        _invalidate_dns_cache(hostname)
        with patch("app.services.upstream_client._default_resolver", new=resolver):
            first = UpstreamClient()
            second = UpstreamClient()
            first_result = asyncio.run(first._resolve_public_addresses(hostname))
            second_result = asyncio.run(second._resolve_public_addresses(hostname))
        _invalidate_dns_cache(hostname)

        self.assertEqual(first_result, (PUBLIC_ADDRESS,))
        self.assertEqual(second_result, (PUBLIC_ADDRESS,))
        self.assertEqual(calls, 1)

    def test_newapi_parses_envelopes_matches_full_key_and_uses_status_price_as_cost(self) -> None:
        seen_headers: list[tuple[str, str | None, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            seen_headers.append(
                (
                    target,
                    request.headers.get("Authorization"),
                    request.headers.get("New-Api-User"),
                )
            )
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "default": {"ratio": 1, "desc": "Default"},
                            "vip": {"ratio": "1.8", "desc": "VIP"},
                        },
                    },
                )
            if target == "/api/token/?p=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": [
                                {"key": "sk-account-complete", "group": "vip"},
                                {"key": "sk-account", "group": "default"},
                            ]
                        },
                    },
                )
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"price": 0.1, "quota_per_unit": 500_000}},
                )
            if target == "/api/user/self":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota": 1_000_000}},
                )
            return httpx.Response(404, json={"message": "missing"})

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            api_key="sk-account-complete",
            access_token="access-token-wins",
            new_api_user=7,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.upstream_type, "newapi")
        self.assertEqual(result.source, "configured")
        self.assertEqual([group.name for group in result.groups], ["default", "vip"])
        self.assertIsNotNone(result.matched_group)
        self.assertEqual(result.matched_group.name, "vip")
        self.assertAlmostEqual(result.discovered_group_multiplier or 0, 1.8)
        self.assertEqual(result.discovered_group_multiplier_source, "self.groups.ratio")
        # ¥1 / $10 is stored as a normalized CNY/USD cost of 0.1.
        self.assertEqual(result.discovered_recharge_multiplier, 0.1)
        self.assertEqual(result.discovered_recharge_multiplier_source, "status.price")
        self.assertEqual(result.balance_remaining, 2.0)
        self.assertEqual(result.balance_status, "ok")
        self.assertTrue(seen_headers)
        status_headers = next(headers for headers in seen_headers if headers[0] == "/api/status")
        self.assertEqual(status_headers[1:], (None, None))
        self.assertTrue(
            all(
                headers[1:] == ("access-token-wins", "7")
                for headers in seen_headers
                if headers[0] not in {"/api/status", NEWAPI_UPTIME_STATUS_ENDPOINT}
            )
        )
        uptime_headers = next(
            headers for headers in seen_headers if headers[0] == NEWAPI_UPTIME_STATUS_ENDPOINT
        )
        self.assertEqual(uptime_headers[1:], (None, None))

    def test_newapi_scalar_group_prefers_authoritative_string_id_after_rename(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": [
                            {"id": "vip", "name": "VIP renamed", "ratio": 1.8},
                            {"id": "gold", "name": "vip", "ratio": 3.0},
                        ],
                    },
                )
            if target == "/api/token/?p=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": [{
                                "id": 41,
                                "key": "sk-account-complete",
                                "group": "vip",
                                "status": 1,
                            }],
                        },
                    },
                )
            return httpx.Response(404, json={"message": "missing"})

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            api_key="sk-account-complete",
            access_token="newapi-management-token",
            new_api_user=7,
            account_api_keys={7: "sk-account-complete"},
        )

        state = result.account_upstream_states[7]
        self.assertEqual(state.group_id, "vip")
        self.assertEqual(state.group_status, "available")
        self.assertEqual(result.account_group_matches[7].id, "vip")
        self.assertEqual(result.account_group_matches[7].name, "VIP renamed")
        self.assertEqual(result.account_group_matches[7].multiplier, 1.8)
        self.assertIsNotNone(result.matched_group)
        self.assertEqual(result.matched_group.id, "vip")
        self.assertEqual(result.matched_group.multiplier, 1.8)

    def test_explicit_group_id_never_falls_back_to_a_same_named_group(self) -> None:
        account_key = "sk-explicit-group-id"

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [
                            {
                                "id": "current-id",
                                "name": "Shared name",
                                "rate_multiplier": 1.5,
                            }
                        ],
                    },
                )
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": 21,
                                    "key": account_key,
                                    "group_id": "deleted-id",
                                    "group_name": "Shared name",
                                    "status": "active",
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            api_key=account_key,
            account_api_keys={7: account_key},
            selected_group_id="deleted-id",
            selected_group_name="Shared name",
        )

        self.assertIsNone(result.matched_group)
        self.assertNotIn(7, result.account_group_matches)
        state = result.account_upstream_states[7]
        self.assertEqual(state.group_id, "deleted-id")
        self.assertEqual(state.group_name, "Shared name")
        self.assertEqual(state.group_status, "deleted")

    def test_group_discovery_preserves_different_ids_with_the_same_name(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": [
                            {"id": "group-a", "name": "Shared", "ratio": 1.5}
                        ],
                    },
                )
            if target == "/api/pricing":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "groups": [
                                {"id": "group-b", "name": "Shared", "ratio": 2.5}
                            ]
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(handler, upstream_type="newapi")

        self.assertEqual(
            [(group.id, group.name, group.multiplier) for group in result.groups],
            [
                ("group-a", "Shared", 1.5),
                ("group-b", "Shared", 2.5),
            ],
        )

    def test_rate_override_with_group_id_does_not_fall_back_to_name(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [
                            {"id": "group-a", "name": "Shared", "rate_multiplier": 1.5}
                        ],
                    },
                )
            if target == "/api/v1/groups/rates":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [
                            {
                                "group_id": "missing-id",
                                "group_name": "Shared",
                                "rate_multiplier": 9.0,
                            }
                        ],
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(handler, upstream_type="sub2api")

        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].id, "group-a")
        self.assertEqual(result.groups[0].multiplier, 1.5)

    def test_newapi_batch_matches_masked_and_full_keys_without_leaking_secrets(self) -> None:
        alpha_key = "sk-local-alpha-secret-Z7Q9"
        beta_key = "sk-local-beta-secret-X6P8"
        legacy_key = "sk-local-legacy-secret-W5N7"
        similar_but_not_equal = "sk-local-alpha-secret"
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "default": {"ratio": 1},
                            "vip": {"ratio": 2},
                        },
                    },
                )
            if target == "/api/token/?p=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": [
                                {"id": 11, "key": "sk**********Z7Q9", "group": "default"},
                                {"id": 12, "key": beta_key, "group": "vip"},
                                {"id": 13, "key": "loca********W5N7", "group": "retired"},
                            ]
                        },
                    },
                )
            if target == "/api/token/11/key":
                self.assertEqual(request.method, "POST")
                return httpx.Response(200, json={"success": True, "data": {"key": alpha_key[3:]}})
            if target == "/api/token/13/key":
                self.assertEqual(request.method, "POST")
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"key": "different-internal-key"}},
                )
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"price": 0.1, "quota_per_unit": 500_000}},
                )
            if target == "/api/user/self":
                return httpx.Response(200, json={"success": True, "data": {"quota": 500_000}})
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="newapi-management-token",
            new_api_user=7,
            account_api_keys={
                7: alpha_key,
                8: beta_key,
                9: similar_but_not_equal,
                10: legacy_key,
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(set(result.account_group_matches), {7, 8})
        self.assertEqual(result.account_group_matches[7].name, "default")
        self.assertEqual(result.account_group_matches[7].multiplier, 1)
        self.assertEqual(result.account_group_matches[8].name, "vip")
        self.assertEqual(result.account_group_matches[8].multiplier, 2)
        targets = [request_target(request) for request in seen]
        for endpoint in NEWAPI_ENDPOINTS:
            if endpoint == NEWAPI_TODAY_USAGE_ENDPOINT:
                self.assertEqual(
                    sum(request.url.path == endpoint for request in seen),
                    2,
                    endpoint,
                )
            else:
                self.assertEqual(targets.count(endpoint), 1, endpoint)
        self.assertEqual(targets.count("/api/token/11/key"), 1)
        self.assertEqual(targets.count("/api/token/12/key"), 0)
        self.assertEqual(targets.count("/api/token/13/key"), 1)

        serialized_result = json.dumps(result.as_dict())
        serialized_requests = json.dumps(
            [
                {
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "body": request.content.decode("utf-8", errors="replace"),
                }
                for request in seen
            ]
        )
        for secret in (alpha_key, beta_key, legacy_key, similar_but_not_equal):
            self.assertNotIn(secret, serialized_result)
            self.assertNotIn(secret, serialized_requests)
        for fragment in ("Z7Q9", "X6P8", "W5N7"):
            self.assertNotIn(fragment, serialized_result)

    def test_masked_key_detail_discovery_covers_the_complete_upstream_page(self) -> None:
        target_key = "sk-sub2-target-secret-T9Z8"
        requested_targets: list[str] = []
        listed_records = [
            {
                "id": record_id,
                "key": "sk****T9Z8",
                "group_id": 2,
            }
            for record_id in range(1, 52)
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            requested_targets.append(target)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 2, "name": "plus", "rate_multiplier": 1.5}],
                    },
                )
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": listed_records}},
                )
            if target.startswith("/api/v1/keys/"):
                record_id = int(target.rsplit("/", 1)[-1])
                revealed_key = (
                    target_key
                    if record_id == 51
                    else f"sk-unrelated-key-{record_id:03d}"
                )
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"key": revealed_key}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2-management-token",
            account_api_keys={7: target_key},
        )

        self.assertTrue(result.ok)
        self.assertIn("/api/v1/keys/51", requested_targets)
        self.assertEqual(result.account_group_matches[7].id, "2")
        self.assertEqual(result.account_group_matches[7].multiplier, 1.5)
        self.assertNotIn(target_key, json.dumps(result.as_dict()))

    def test_newapi_masked_fallback_rejects_ambiguous_candidates(self) -> None:
        payloads = {
            "/api/token/?p=1&page_size=200": {
                "success": True,
                "data": {
                    "items": [
                        {"id": 11, "key": "sk**********Z7Q9", "group": "default"},
                    ]
                },
            }
        }

        matches = _unique_masked_api_key_records(
            "newapi",
            payloads,
            {
                "sk-first-private-Z7Q9",
                "sk-second-private-Z7Q9",
            },
        )

        self.assertEqual(matches, {})

    def test_newapi_balance_uses_dynamic_quota_unit_and_explicit_totals(self) -> None:
        seen_headers: dict[str, tuple[str | None, str | None]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            seen_headers[target] = (
                request.headers.get("Authorization"),
                request.headers.get("New-Api-User"),
            )
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota_per_unit": 1_000_000, "price": 0.1}},
                )
            if target == "/api/user/self":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "quota": 12_500_000,
                            "used_quota": 2_500_000,
                            "total_quota": 20_000_000,
                        },
                    },
                )
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            api_key="sk-model-key",
            access_token="console-access-token",
            new_api_user=42,
            selected_group_name="default",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.balance_remaining, 12.5)
        self.assertEqual(result.balance_used, 2.5)
        self.assertEqual(result.balance_total, 20.0)
        self.assertEqual(result.balance_unit, "USD")
        self.assertEqual(result.balance_status, "ok")
        self.assertEqual(seen_headers["/api/status"], (None, None))
        self.assertEqual(
            seen_headers["/api/user/self"],
            ("console-access-token", "42"),
        )
        self.assertNotIn("/api/usage/token", seen_headers)

    def test_newapi_today_usage_reads_self_log_stat_in_configured_timezone(self) -> None:
        seen_usage_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if request.url.path == NEWAPI_TODAY_USAGE_ENDPOINT:
                seen_usage_requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"quota": 2_500_000 if len(seen_usage_requests) == 1 else 1_250_000},
                    },
                )
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota_per_unit": 1_000_000}},
                )
            if target == "/api/user/self":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota": 12_500_000}},
                )
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="console-access-token",
            new_api_user=42,
            today_timezone="America/New_York",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.today_balance_used, 2.5)
        self.assertEqual(result.today_balance_unit, "USD")
        self.assertEqual(result.today_balance_status, "ok")
        self.assertIsNone(result.today_balance_error)
        self.assertEqual(result.yesterday_balance_used, 1.25)
        self.assertEqual(result.yesterday_balance_unit, "USD")
        self.assertEqual(result.yesterday_balance_status, "ok")
        self.assertIsNone(result.yesterday_balance_error)
        self.assertEqual(len(seen_usage_requests), 2)
        today_request, yesterday_request = seen_usage_requests
        for request in seen_usage_requests:
            self.assertEqual(request.headers.get("Authorization"), "console-access-token")
            self.assertEqual(request.headers.get("New-Api-User"), "42")
            self.assertEqual(
                set(request.extensions["timeout"].values()),
                {UPSTREAM_USAGE_TIMEOUT_SECONDS},
            )
        start_timestamp = int(today_request.url.params["start_timestamp"])
        end_timestamp = int(today_request.url.params["end_timestamp"])
        zone = ZoneInfo("America/New_York")
        start = datetime.fromtimestamp(start_timestamp, zone)
        end = datetime.fromtimestamp(end_timestamp, zone)
        self.assertEqual((start.hour, start.minute, start.second), (0, 0, 0))
        self.assertEqual(start.date(), end.date())
        self.assertGreaterEqual(end_timestamp, start_timestamp)
        yesterday_start = datetime.fromtimestamp(
            int(yesterday_request.url.params["start_timestamp"]),
            zone,
        )
        yesterday_end = datetime.fromtimestamp(
            int(yesterday_request.url.params["end_timestamp"]),
            zone,
        )
        self.assertEqual((yesterday_start.hour, yesterday_start.minute, yesterday_start.second), (0, 0, 0))
        self.assertEqual((yesterday_end.hour, yesterday_end.minute, yesterday_end.second), (23, 59, 59))
        self.assertEqual(yesterday_start.date(), start.date() - timedelta(days=1))
        self.assertEqual(yesterday_end.date(), yesterday_start.date())

    def test_newapi_today_usage_marks_missing_optional_endpoint_unsupported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if request.url.path == NEWAPI_TODAY_USAGE_ENDPOINT:
                return httpx.Response(404)
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota_per_unit": 1_000_000}},
                )
            if target == "/api/user/self":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota": 12_500_000}},
                )
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="console-access-token",
            new_api_user=42,
        )

        self.assertTrue(result.ok)
        self.assertIsNone(result.today_balance_used)
        self.assertIsNone(result.today_balance_unit)
        self.assertEqual(result.today_balance_status, "unsupported")
        self.assertIsNone(result.yesterday_balance_used)
        self.assertIsNone(result.yesterday_balance_unit)
        self.assertEqual(result.yesterday_balance_status, "unsupported")

    def test_sub2api_today_usage_is_requested_once_and_reports_failure_reason(self) -> None:
        cases = (
            ("http_503", "http_503"),
            ("http_429", "http_429"),
            ("http_401", "http_401"),
            ("timeout", "timeout"),
            ("invalid_json", "invalid_json"),
            ("invalid_payload", "invalid_payload"),
        )
        for failure, expected_error in cases:
            with self.subTest(failure=failure):
                attempts = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    target = request_target(request)
                    if target == "/api/v1/groups/available":
                        return httpx.Response(200, json={"code": 0, "data": []})
                    if target == SUB2API_TODAY_USAGE_ENDPOINT:
                        attempts += 1
                        if failure == "timeout":
                            raise httpx.ReadTimeout(
                                "usage timed out",
                                request=request,
                            )
                        if failure == "invalid_json":
                            return httpx.Response(200, content=b"not-json")
                        if failure == "invalid_payload":
                            return httpx.Response(200, json={"code": 0, "data": {}})
                        return httpx.Response(int(failure.removeprefix("http_")))
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    access_token="sub2api-login-token",
                )

                self.assertEqual(attempts, 1)
                self.assertIsNone(result.today_balance_used)
                self.assertEqual(result.today_balance_status, "error")
                self.assertEqual(result.today_balance_error, expected_error)

    def test_usage_timeout_preserves_longer_caller_timeout(self) -> None:
        seen_read_timeouts: dict[str, float] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            timeout = request.extensions.get("timeout") or {}
            if "read" in timeout:
                seen_read_timeouts[request.url.path] = float(timeout["read"])
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": 21,
                                    "key": "sk-long-timeout",
                                    "status": "active",
                                }
                            ]
                        },
                    },
                )
            if target == SUB2API_TODAY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"today_actual_cost": 3.25}},
                )
            if request.url.path == SUB2API_USAGE_STATS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"total_actual_cost": 2.75}},
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "21": {
                                    "api_key_id": 21,
                                    "today_actual_cost": 1.25,
                                }
                            }
                        },
                    },
                )
            return httpx.Response(404)

        client = UpstreamClient(
            timeout_seconds=75.0,
            transport=httpx.MockTransport(handler),
            resolver=public_resolver,
        )
        result = asyncio.run(
            client.discover(
                "https://upstream.example",
                upstream_type="sub2api",
                access_token="sub2api-login-token",
                account_api_keys={7: "sk-long-timeout"},
            )
        )

        self.assertEqual(result.today_balance_status, "ok")
        self.assertEqual(UPSTREAM_USAGE_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(seen_read_timeouts[SUB2API_TODAY_USAGE_ENDPOINT], 75.0)
        self.assertEqual(seen_read_timeouts[SUB2API_USAGE_STATS_ENDPOINT], 75.0)
        self.assertEqual(seen_read_timeouts[SUB2API_API_KEY_USAGE_ENDPOINT], 75.0)

    def test_newapi_today_usage_time_range_falls_back_to_default_timezone(self) -> None:
        now = datetime(2026, 7, 18, 20, 30, tzinfo=timezone.utc)

        params = _newapi_today_usage_params("not/a-zone", now=now)

        zone = ZoneInfo("Asia/Shanghai")
        self.assertEqual(
            params,
            {
                "start_timestamp": int(
                    datetime(2026, 7, 19, 0, 0, tzinfo=zone).timestamp()
                ),
                "end_timestamp": int(now.timestamp()),
            },
        )
        self.assertEqual(
            _newapi_yesterday_usage_params("not/a-zone", now=now),
            {
                "start_timestamp": int(
                    datetime(2026, 7, 18, 0, 0, tzinfo=zone).timestamp()
                ),
                "end_timestamp": int(
                    datetime(2026, 7, 19, 0, 0, tzinfo=zone).timestamp()
                ) - 1,
            },
        )

    def test_newapi_balance_falls_back_to_default_quota_unit_and_accepts_used_alias(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota_per_unit": 0}},
                )
            if target == "/api/user/self":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"quota": 738_170_000, "quota_used": 61_830_000},
                    },
                )
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="console-access-token",
            new_api_user="42",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.balance_remaining, 1476.34)
        self.assertEqual(result.balance_used, 123.66)
        self.assertIsNone(result.balance_total)
        self.assertEqual(result.balance_status, "ok")

    def test_newapi_balance_never_uses_model_api_key_as_console_access_token(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            seen.append(target)
            if target == "/api/user/self":
                self.fail("NewAPI user balance must not be requested with a model API key")
            if target == "/api/user/self/groups":
                self.assertEqual(request.headers.get("Authorization"), "Bearer sk-model-key")
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            if target == "/api/status":
                self.assertIsNone(request.headers.get("Authorization"))
                return httpx.Response(200, json={"success": True, "data": {}})
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            api_key="sk-model-key",
            new_api_user=42,
            selected_group_name="default",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.discovered_group_multiplier, 1.0)
        self.assertEqual(result.balance_status, "credentials_missing")
        self.assertIsNone(result.balance_remaining)
        self.assertNotIn("/api/user/self", seen)

    def test_newapi_balance_requires_numeric_user_id_without_blocking_groups(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            seen.append(target)
            if target == "/api/user/self":
                self.fail("NewAPI balance must not be requested without a numeric user ID")
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 2}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="console-access-token",
            new_api_user="not-a-user-id",
            selected_group_name="default",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.discovered_group_multiplier, 2.0)
        self.assertEqual(result.balance_status, "credentials_missing")
        self.assertIsNone(result.balance_remaining)
        self.assertNotIn("/api/user/self", seen)

    def test_newapi_balance_rejects_unsuccessful_and_invalid_payloads(self) -> None:
        cases = (
            {"success": False, "data": {"quota": 500_000}},
            {"success": True, "data": {"quota": True}},
            {"success": True, "data": {"quota": 500_000, "used_quota": "NaN"}},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                def handler(request: httpx.Request) -> httpx.Response:
                    target = request_target(request)
                    if target == "/api/status":
                        return httpx.Response(
                            200,
                            json={"success": True, "data": {"quota_per_unit": 500_000}},
                        )
                    if target == "/api/user/self":
                        return httpx.Response(200, json=payload)
                    if target == "/api/user/self/groups":
                        return httpx.Response(
                            200,
                            json={"success": True, "data": {"default": {"ratio": 1}}},
                        )
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="newapi",
                    access_token="console-access-token",
                    new_api_user=42,
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.balance_status, "error")
                self.assertIsNone(result.balance_remaining)

    def test_sub2api_balance_uses_login_access_token_without_newapi_header(self) -> None:
        seen_headers: dict[str, tuple[str | None, str | None]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            seen_headers[target] = (
                request.headers.get("Authorization"),
                request.headers.get("New-Api-User"),
            )
            if target == "/api/v1/auth/me":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"balance": "42.75"}},
                )
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 1, "name": "default", "rate_multiplier": 1}],
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            api_key="sk-model-key",
            access_token="sub2api-login-token",
            new_api_user=99,
            selected_group_id=1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.discovered_group_multiplier, 1.0)
        self.assertEqual(result.balance_remaining, 42.75)
        self.assertIsNone(result.balance_total)
        self.assertIsNone(result.balance_used)
        self.assertEqual(result.balance_unit, "USD")
        self.assertEqual(result.balance_status, "ok")
        self.assertEqual(
            seen_headers["/api/v1/auth/me"],
            ("Bearer sub2api-login-token", None),
        )

    def test_sub2api_balance_does_not_use_model_api_key(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            seen.append(target)
            if target == "/api/v1/auth/me":
                self.fail("Sub2API user balance must not be requested with a model API key")
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json=[{"id": 1, "name": "default", "rate_multiplier": 1}],
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            api_key="sk-model-key",
            selected_group_id=1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.discovered_group_multiplier, 1.0)
        self.assertEqual(result.balance_status, "credentials_missing")
        self.assertNotIn("/api/v1/auth/me", seen)

    def test_sub2api_auth_rejection_only_tracks_auth_me_401(self) -> None:
        def auth_me_rejected(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/auth/me":
                return httpx.Response(401)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 1, "name": "default", "rate_multiplier": 1}],
                    },
                )
            return httpx.Response(404)

        rejected = self.run_discovery(
            auth_me_rejected,
            upstream_type="sub2api",
            access_token="expired-login-token",
        )

        self.assertTrue(rejected.ok)
        self.assertTrue(rejected.sub2api_auth_rejected)

        def other_endpoint_rejected(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/auth/me":
                return httpx.Response(200, json={"code": 0, "data": {"balance": 9.5}})
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 1, "name": "default", "rate_multiplier": 1}],
                    },
                )
            if target == "/api/v1/groups/rates":
                return httpx.Response(401)
            return httpx.Response(404)

        not_rejected = self.run_discovery(
            other_endpoint_rejected,
            upstream_type="sub2api",
            access_token="valid-login-token",
        )

        self.assertTrue(not_rejected.ok)
        self.assertFalse(not_rejected.sub2api_auth_rejected)

    def test_newapi_401_never_sets_sub2api_auth_rejection(self) -> None:
        result = self.run_discovery(
            lambda _request: httpx.Response(401),
            upstream_type="newapi",
            access_token="newapi-console-token",
            new_api_user=42,
        )

        self.assertEqual(result.status, "error")
        self.assertFalse(result.sub2api_auth_rejected)

    def test_selected_group_overrides_group_from_matched_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "default": {"ratio": 1},
                            "vip": {"ratio": 2},
                        },
                        "success": True,
                    },
                )
            if target == "/api/token/search?p=1&size=200":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"records": [{"key": "sk-live", "group": "vip"}]}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            api_key="sk-live",
            selected_group_name="default",
        )

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.matched_group)
        self.assertEqual(result.matched_group.name, "default")
        self.assertEqual(result.discovered_group_multiplier, 1.0)

    def test_newapi_explicit_payment_multiplier_precedes_status_price_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"price": 5}},
                )
            if target == "/api/v1/payment/config":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"balance_recharge_multiplier": 2.5}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            selected_group_name="default",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.discovered_recharge_multiplier, 0.4)
        self.assertEqual(
            result.discovered_recharge_multiplier_source,
            "payment.config.balance_recharge_multiplier",
        )

    def test_newapi_status_explicit_recharge_field_precedes_price(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"price": 5, "balance_recharge_multiplier": 2.5},
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            selected_group_name="default",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.discovered_recharge_multiplier, 0.4)
        self.assertEqual(
            result.discovered_recharge_multiplier_source,
            "status.balance_recharge_multiplier",
        )

    def test_newapi_status_invalid_explicit_recharge_is_not_masked_by_price(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            if target == "/api/status":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"price": 5, "balance_recharge_multiplier": 0},
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(handler, upstream_type="newapi")

        self.assertEqual(result.status, "error")
        self.assertIsNone(result.discovered_recharge_multiplier)

    def test_sub2api_rates_override_available_group_and_checkout_precedes_config(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "data": [
                            {"id": 1, "name": "standard", "rate_multiplier": 1},
                            {"id": 2, "name": "premium", "rate_multiplier": 1.5},
                        ],
                    },
                )
            if target == "/api/v1/groups/rates":
                return httpx.Response(200, json={"result": {"2": "2.75"}, "success": True})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={"data": {"items": [{"api_key": "sk-sub2", "group_id": 2}]}, "code": 0},
                )
            if target == "/api/v1/payment/checkout-info":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"balance_recharge_multiplier": 10}},
                )
            if target == "/api/v1/payment/config":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"balance_recharge_multiplier": 9.9}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            api_key="sk-sub2",
        )

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.matched_group)
        self.assertEqual(result.matched_group.id, "2")
        self.assertEqual(result.matched_group.name, "premium")
        self.assertAlmostEqual(result.discovered_group_multiplier or 0, 2.75)
        self.assertEqual(result.discovered_group_multiplier_source, "groups.rates")
        # Sub2API exposes USD credited per CNY. 1 CNY = 10 USD costs 0.1 CNY/USD.
        self.assertAlmostEqual(result.discovered_recharge_multiplier or 0, 0.1)
        self.assertEqual(
            result.discovered_recharge_multiplier_source,
            "payment.checkout-info.balance_recharge_multiplier",
        )

    def test_sub2api_unique_mask_requires_full_detail_before_group_binding(self) -> None:
        account_key = "sk-sub2-account-secret-Q4M6"
        reveal_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal reveal_calls
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 2, "name": "premium", "rate_multiplier": 1.5}],
                    },
                )
            if target == "/api/v1/groups/rates":
                return httpx.Response(200, json={"code": 0, "data": {"2": 2.75}})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": [{"id": 21, "key": "sk****Q4M6", "group_id": 2}]}},
                )
            if target == "/api/v1/keys/21":
                reveal_calls += 1
                self.assertEqual(request.method, "GET")
                return httpx.Response(200, json={"code": 0, "data": {"key": account_key}})
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2-management-token",
            account_api_keys={7: account_key},
        )

        self.assertTrue(result.ok)
        self.assertEqual(reveal_calls, 1)
        self.assertEqual(result.account_group_matches[7].id, "2")
        self.assertEqual(result.account_group_matches[7].name, "premium")
        self.assertEqual(result.account_group_matches[7].multiplier, 2.75)
        self.assertNotIn(account_key, json.dumps(result.as_dict()))

    def test_sub2api_masked_key_detail_must_return_a_full_exact_key(self) -> None:
        account_key = "sk-sub2-account-secret-Q4M6"
        for detail_payload in (
            None,
            {"code": 0, "data": {"id": 21, "key": "sk****Q4M6"}},
        ):
            with self.subTest(detail_payload=detail_payload):
                detail_requests: list[str] = []
                usage_requests: list[str] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    target = request_target(request)
                    if target == "/api/v1/groups/available":
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": [
                                    {"id": 2, "name": "premium", "rate_multiplier": 1.5}
                                ],
                            },
                        )
                    if target == "/api/v1/keys?page=1&page_size=200":
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": {
                                    "items": [
                                        {
                                            "id": 21,
                                            "key": "sk****Q4M6",
                                            "group_id": 2,
                                            "status": "active",
                                        }
                                    ],
                                    "total": 1,
                                },
                            },
                        )
                    if target == "/api/v1/keys/21":
                        detail_requests.append(target)
                        if detail_payload is None:
                            return httpx.Response(503)
                        return httpx.Response(200, json=detail_payload)
                    if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                        usage_requests.append(request.url.path)
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": {
                                    "stats": {
                                        "21": {
                                            "api_key_id": 21,
                                            "today_actual_cost": 4.25,
                                        }
                                    }
                                },
                            },
                        )
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    api_key=account_key,
                    access_token="sub2-management-token",
                    account_api_keys={7: account_key},
                )

                self.assertEqual(detail_requests, ["/api/v1/keys/21"])
                self.assertEqual(usage_requests, [])
                self.assertIsNone(result.matched_group)
                self.assertIsNone(result.matched_account_state)
                self.assertNotIn(7, result.account_group_matches)
                self.assertNotIn(7, result.account_upstream_states)

    def test_sub2api_failed_pagination_cannot_make_a_masked_candidate_authoritative(self) -> None:
        account_key = "sk-sub2-account-secret-Q4M6"
        requested_targets: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            requested_targets.append(target)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 2, "name": "premium", "rate_multiplier": 1.5}],
                    },
                )
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 21, "key": "sk****Q4M6", "group_id": 2}
                            ],
                            "page": 1,
                            "page_size": 200,
                            "pages": 2,
                        },
                    },
                )
            if target == "/api/v1/keys?page=2&page_size=200":
                return httpx.Response(503)
            if target == "/api/v1/keys/21":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"id": 21, "key": "sk-another-account-Q4M6"},
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2-management-token",
            account_api_keys={7: account_key},
        )

        self.assertIn("/api/v1/keys?page=2&page_size=200", requested_targets)
        self.assertIn("/api/v1/keys/21", requested_targets)
        self.assertNotIn(7, result.account_group_matches)
        self.assertNotIn(7, result.account_upstream_states)

    def test_sub2api_ignores_orphaned_numeric_group_ids_without_name_or_rate(self) -> None:
        valid_key = "sk-sub2-valid-secret-A1B2"
        orphan_55_key = "sk-sub2-orphan-secret-C3D4"
        orphan_72_key = "sk-sub2-orphan-secret-E5F6"
        revealed_keys = {
            21: valid_key,
            22: orphan_55_key,
            23: orphan_72_key,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 2, "name": "premium", "rate_multiplier": 1.5}],
                    },
                )
            if target == "/api/v1/groups/rates":
                return httpx.Response(200, json={"code": 0, "data": {"2": 2.75}})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 21, "key": "sk****A1B2", "group_id": 2},
                                {"id": 22, "key": "sk****C3D4", "group_id": 55},
                                {"id": 23, "key": "sk****E5F6", "group_id": 72},
                            ]
                        },
                    },
                )
            if target.startswith("/api/v1/keys/"):
                record_id = int(target.rsplit("/", 1)[-1])
                return httpx.Response(200, json={"code": 0, "data": {"key": revealed_keys[record_id]}})
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2-management-token",
            account_api_keys={
                7: valid_key,
                8: orphan_55_key,
                9: orphan_72_key,
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(set(result.account_group_matches), {7})
        self.assertEqual(result.account_group_matches[7].name, "premium")
        self.assertEqual(result.account_group_matches[7].multiplier, 2.75)
        serialized = json.dumps(result.as_dict())
        self.assertNotIn(orphan_55_key, serialized)
        self.assertNotIn(orphan_72_key, serialized)

    def test_auto_detects_sub2api(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json=[{"id": 5, "name": "auto", "rate_multiplier": 3}],
                )
            if target == "/api/v1/groups/rates":
                return httpx.Response(200, json={"5": 3.5})
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="auto",
            selected_group_id=5,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.upstream_type, "sub2api")
        self.assertEqual(result.source, "auto")
        self.assertEqual(result.discovered_group_multiplier, 3.5)

    def test_missing_optional_values_remain_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request_target(request) == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": "auto"}}},
                )
            if request_target(request) == "/api/v1/payment/config":
                return httpx.Response(200, json={"success": True, "data": {}})
            return httpx.Response(404)

        result = self.run_discovery(handler, upstream_type="newapi", api_key="sk-not-listed")

        self.assertTrue(result.ok)
        self.assertEqual(result.groups, [])
        self.assertIsNone(result.matched_group)
        self.assertIsNone(result.discovered_group_multiplier)
        self.assertIsNone(result.discovered_group_multiplier_source)
        self.assertIsNone(result.discovered_recharge_multiplier)
        self.assertIsNone(result.discovered_recharge_multiplier_source)
        self.assertEqual(result.recharge_discovery_status, "missing")

    def test_successful_groups_with_no_successful_recharge_endpoint_is_not_missing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request_target(request) == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json=[{"id": 1, "name": "standard", "rate_multiplier": 2}],
                )
            return httpx.Response(503)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            selected_group_id=1,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.discovered_group_multiplier, 2.0)
        self.assertIsNone(result.discovered_recharge_multiplier)
        self.assertEqual(result.recharge_discovery_status, "error")

    def test_present_but_invalid_recharge_multiplier_fails_discovery(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json=[{"id": 1, "name": "standard", "rate_multiplier": 2}],
                )
            if target == "/api/v1/payment/checkout-info":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"balance_recharge_multiplier": 0}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            selected_group_id=1,
        )

        self.assertEqual(result.status, "error")
        self.assertIsNone(result.discovered_recharge_multiplier)
        self.assertNotIn("0", result.message)

    def test_transport_connects_to_pinned_ip_and_preserves_host_and_tls_sni(self) -> None:
        seen: list[tuple[str, str | None, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(
                (
                    request.url.host,
                    request.headers.get("Host"),
                    request.extensions.get("sni_hostname"),
                )
            )
            if request_target(request) == "/api/v1/groups/available":
                return httpx.Response(
                    200,
                    json=[{"id": 1, "name": "standard", "rate_multiplier": 1}],
                )
            return httpx.Response(404)

        resolver_calls = 0

        def resolver(_hostname: str) -> list[str]:
            nonlocal resolver_calls
            resolver_calls += 1
            return [PUBLIC_ADDRESS]

        client = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        )
        result = asyncio.run(
            client.discover(
                "https://upstream.example",
                upstream_type="sub2api",
                selected_group_id=1,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(resolver_calls, 1)
        self.assertTrue(seen)
        self.assertTrue(all(host == PUBLIC_ADDRESS for host, _, _ in seen))
        self.assertTrue(all(host_header == "upstream.example" for _, host_header, _ in seen))
        self.assertTrue(all(sni == "upstream.example" for _, _, sni in seen))

    def test_pinned_transport_falls_back_only_after_all_requests_fail_to_connect(self) -> None:
        seen_hosts: list[str] = []

        class CountingTransport(httpx.AsyncBaseTransport):
            def __init__(self) -> None:
                self.close_calls = 0

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                seen_hosts.append(request.url.host)
                if request.url.host == PUBLIC_ADDRESS:
                    raise httpx.ConnectError("first pinned address unavailable", request=request)
                if request_target(request) == "/api/v1/groups/available":
                    return httpx.Response(
                        200,
                        json=[{"id": 1, "name": "standard", "rate_multiplier": 1}],
                    )
                if request_target(request) == "/api/v1/payment/checkout-info":
                    return httpx.Response(200, json={"code": 0, "data": {}})
                return httpx.Response(404)

            async def aclose(self) -> None:
                self.close_calls += 1

        transport = CountingTransport()
        client = UpstreamClient(
            transport=transport,
            resolver=lambda _hostname: [PUBLIC_ADDRESS, SECOND_PUBLIC_ADDRESS],
        )
        result = asyncio.run(
            client.discover(
                "https://upstream.example",
                upstream_type="sub2api",
                selected_group_id=1,
            )
        )

        self.assertTrue(result.ok)
        self.assertIn(PUBLIC_ADDRESS, seen_hosts)
        self.assertIn(SECOND_PUBLIC_ADDRESS, seen_hosts)
        first_second_index = seen_hosts.index(SECOND_PUBLIC_ADDRESS)
        self.assertTrue(all(host == PUBLIC_ADDRESS for host in seen_hosts[:first_second_index]))
        self.assertEqual(transport.close_calls, 1)

    def test_pinned_transport_stops_after_any_http_response(self) -> None:
        seen_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host)
            return httpx.Response(503)

        client = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=lambda _hostname: [PUBLIC_ADDRESS, SECOND_PUBLIC_ADDRESS],
        )
        result = asyncio.run(client.discover("https://upstream.example", upstream_type="sub2api"))

        self.assertEqual(result.status, "error")
        self.assertTrue(seen_hosts)
        self.assertTrue(all(host == PUBLIC_ADDRESS for host in seen_hosts))

    def test_upstream_group_text_cannot_reflect_api_key_or_access_token(self) -> None:
        api_key = "sk-reflected-api-key"
        access_token = "access-reflected-token"

        def handler(request: httpx.Request) -> httpx.Response:
            if request_target(request) == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            api_key: {
                                "ratio": 2,
                                "desc": f"description contains {access_token}",
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            api_key=api_key,
            access_token=access_token,
            selected_group_id=api_key,
        )

        serialized = json.dumps(result.as_dict())
        self.assertTrue(result.ok)
        self.assertNotIn(api_key, serialized)
        self.assertNotIn(access_token, serialized)
        self.assertIsNotNone(result.matched_group)

    def test_private_and_reserved_dns_results_are_blocked_before_transport(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        for address in ("127.0.0.1", "10.0.0.2", "169.254.1.1", "192.0.2.10", "::1"):
            client = UpstreamClient(
                transport=httpx.MockTransport(handler),
                resolver=lambda _hostname, address=address: [address],
            )
            result = asyncio.run(client.discover("https://upstream.example", upstream_type="newapi"))
            self.assertEqual(result.status, "error")
            self.assertNotIn("upstream.example", result.message)

        direct = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=public_resolver,
        )
        direct_result = asyncio.run(direct.discover("http://127.0.0.1:8080", upstream_type="sub2api"))
        self.assertEqual(direct_result.status, "insecure_url")
        self.assertEqual(calls, 0)

    def test_public_dns_results_are_capped_before_endpoint_requests(self) -> None:
        client = UpstreamClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
            resolver=lambda _hostname: [f"8.8.8.{index}" for index in range(1, 21)],
        )

        addresses = asyncio.run(client._resolve_public_addresses("upstream.example"))

        self.assertEqual(len(addresses), 8)
        self.assertEqual(addresses[0], "8.8.8.1")

    def test_url_userinfo_query_fragment_and_non_http_schemes_are_rejected(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        client = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=public_resolver,
        )
        invalid_urls = (
            "https://user:password@upstream.example",
            "https://upstream.example?token=secret",
            "https://upstream.example/#fragment",
            "file:///etc/passwd",
        )
        for invalid_url in invalid_urls:
            result = asyncio.run(client.discover(invalid_url, upstream_type="newapi"))
            self.assertEqual(result.status, "error")
        self.assertEqual(calls, 0)

    def test_plain_http_is_rejected_before_transport_even_without_credentials(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        client = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=public_resolver,
        )
        result = asyncio.run(
            client.discover(
                "http://upstream.example",
                upstream_type="sub2api",
            )
        )

        self.assertEqual(result.status, "insecure_url")
        self.assertEqual(calls, 0)

    def test_redirect_is_not_followed(self) -> None:
        targets: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            targets.append(request_target(request))
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1/private?api_key=redirect-secret"},
                content=b"sk-body-secret",
            )

        result = self.run_discovery(handler, upstream_type="sub2api", api_key="sk-header-secret")

        self.assertEqual(result.status, "error")
        self.assertIn("HTTP 302", result.message)
        self.assertFalse(any(target.startswith("/private") for target in targets))
        self.assertEqual(len(targets), 6)

    def test_http_and_network_errors_do_not_disclose_body_url_query_or_api_key(self) -> None:
        secret = "sk-never-echo-this"
        response_body = f"credential rejected: {secret}; /path?token={secret}"

        def http_error(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=response_body.encode())

        result = self.run_discovery(http_error, upstream_type="newapi", api_key=secret)
        serialized = json.dumps(result.as_dict())
        self.assertIn("HTTP 401", result.message)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("token=", serialized)
        self.assertNotIn(response_body, serialized)

        def network_error(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"connection failed with {secret}", request=request)

        network_result = self.run_discovery(network_error, upstream_type="sub2api", api_key=secret)
        network_serialized = json.dumps(network_result.as_dict())
        self.assertEqual(network_result.status, "error")
        self.assertNotIn(secret, network_serialized)

    def test_response_size_is_limited_and_v1_base_path_is_normalized(self) -> None:
        targets: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            targets.append(request_target(request))
            if request_target(request) == "/api/user/self/groups":
                return httpx.Response(200, content=b'"' + b"x" * MAX_RESPONSE_BYTES + b'"')
            return httpx.Response(404)

        client = UpstreamClient(
            transport=httpx.MockTransport(handler),
            resolver=public_resolver,
        )
        result = asyncio.run(
            client.discover(
                "https://upstream.example/api/v1/",
                upstream_type="newapi",
            )
        )

        self.assertEqual(result.status, "error")
        self.assertIn("/api/user/self/groups", targets)
        self.assertFalse(any(target.startswith("/api/v1/api/") for target in targets))

    def test_sub2api_reports_disabled_key_and_available_group_authoritatively(self) -> None:
        seen_today_headers: list[tuple[str | None, str | None]] = []
        seen_today_requests: list[httpx.Request] = []
        seen_yesterday_requests: list[httpx.Request] = []
        seen_key_usage_requests: list[httpx.Request] = []
        seen_core_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                seen_core_requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [{"id": 2, "name": "Plus", "rate_multiplier": 0.5}],
                    },
                )
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": 21,
                                    "key": "sk-disabled-key",
                                    "status": "disabled",
                                    "group_id": 2,
                                    "quota_used": 999.0,
                                }
                            ]
                        },
                    },
                )
            if target == SUB2API_TODAY_USAGE_ENDPOINT:
                seen_today_requests.append(request)
                seen_today_headers.append(
                    (
                        request.headers.get("Authorization"),
                        request.headers.get("New-Api-User"),
                    )
                )
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "today_actual_cost": 3.25,
                        },
                    },
                )
            if request.url.path == SUB2API_USAGE_STATS_ENDPOINT:
                seen_yesterday_requests.append(request)
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"total_actual_cost": 2.75}},
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                seen_key_usage_requests.append(request)
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.content), {"api_key_ids": [21]})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "21": {
                                    "api_key_id": 21,
                                    "today_actual_cost": 12.375,
                                    "total_actual_cost": 999.0,
                                }
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-disabled-key"},
        )

        self.assertTrue(result.ok)
        state = result.account_upstream_states[7]
        self.assertEqual(state.key_status, "disabled")
        self.assertEqual(state.group_status, "available")
        self.assertEqual(state.group_id, "2")
        self.assertEqual(state.usage_amount, 12.375)
        self.assertEqual(state.usage_unit, "USD")
        self.assertEqual(result.today_balance_used, 3.25)
        self.assertEqual(result.today_balance_unit, "USD")
        self.assertEqual(result.today_balance_status, "ok")
        self.assertEqual(result.yesterday_balance_used, 2.75)
        self.assertEqual(result.yesterday_balance_unit, "USD")
        self.assertEqual(result.yesterday_balance_status, "ok")
        self.assertEqual(seen_today_headers, [("Bearer sub2api-login-token", None)])
        self.assertEqual(len(seen_yesterday_requests), 1)
        self.assertEqual(len(seen_key_usage_requests), 1)
        for request in (
            *seen_today_requests,
            *seen_yesterday_requests,
            *seen_key_usage_requests,
        ):
            self.assertEqual(request.headers.get("Authorization"), "Bearer sub2api-login-token")
            self.assertIsNone(request.headers.get("New-Api-User"))
            self.assertEqual(
                set(request.extensions["timeout"].values()),
                {UPSTREAM_USAGE_TIMEOUT_SECONDS},
            )
        self.assertEqual(
            set(seen_core_requests[0].extensions["timeout"].values()),
            {3.5},
        )

    def test_sub2api_today_key_usage_rejects_invalid_values(self) -> None:
        cases = (-1, "nan", True, None)
        for today_actual_cost in cases:
            with self.subTest(today_actual_cost=today_actual_cost):
                def handler(request: httpx.Request) -> httpx.Response:
                    target = request_target(request)
                    if target == "/api/v1/groups/available":
                        return httpx.Response(200, json={"code": 0, "data": []})
                    if target == "/api/v1/keys?page=1&page_size=200":
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": {
                                    "items": [
                                        {
                                            "id": 31,
                                            "key": "sk-invalid-usage",
                                            "status": "active",
                                        }
                                    ]
                                },
                            },
                        )
                    if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                        stats = {
                            "31": {
                                "api_key_id": 31,
                                "today_actual_cost": today_actual_cost,
                            }
                        }
                        return httpx.Response(200, json={"code": 0, "data": {"stats": stats}})
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    access_token="sub2api-login-token",
                    account_api_keys={11: "sk-invalid-usage"},
                )
                self.assertIsNone(result.account_upstream_states[11].usage_amount)

    def test_sub2api_today_key_usage_failure_never_reuses_cumulative_quota(self) -> None:
        for usage_response in (
            httpx.Response(404),
            httpx.Response(500),
            httpx.Response(200, json={"code": 0, "data": {"stats": {}}}),
        ):
            with self.subTest(status_code=usage_response.status_code):
                def handler(request: httpx.Request) -> httpx.Response:
                    target = request_target(request)
                    if target == "/api/v1/groups/available":
                        return httpx.Response(200, json={"code": 0, "data": []})
                    if target == "/api/v1/keys?page=1&page_size=200":
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": {
                                    "items": [
                                        {
                                            "id": 32,
                                            "key": "sk-failed-usage",
                                            "status": "active",
                                            "quota_used": 321.5,
                                        }
                                    ]
                                },
                            },
                        )
                    if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                        return usage_response
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    access_token="sub2api-login-token",
                    account_api_keys={13: "sk-failed-usage"},
                )
                self.assertIsNone(result.account_upstream_states[13].usage_amount)
                self.assertIsNone(result.account_upstream_states[13].usage_unit)

    def test_sub2api_yesterday_usage_uses_date_range_and_timezone(self) -> None:
        fixed_now = datetime(2026, 7, 19, 12, 30, tzinfo=timezone.utc)
        self.assertEqual(
            _sub2api_yesterday_usage_params("America/New_York", now=fixed_now),
            {
                "start_date": "2026-07-18",
                "end_date": "2026-07-18",
                "timezone": "America/New_York",
            },
        )
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request_target(request) == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if request.url.path == SUB2API_USAGE_STATS_ENDPOINT:
                seen.append(request)
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"total_actual_cost": 0}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            today_timezone="America/New_York",
        )

        self.assertEqual(result.yesterday_balance_used, 0)
        self.assertEqual(result.yesterday_balance_status, "ok")
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.params["start_date"], request.url.params["end_date"])
        self.assertEqual(request.url.params["timezone"], "America/New_York")
        self.assertEqual(request.headers.get("Authorization"), "Bearer sub2api-login-token")

    def test_sub2api_yesterday_usage_distinguishes_unsupported_and_failure(self) -> None:
        for status_code, expected_status in ((404, "unsupported"), (405, "unsupported"), (500, "error")):
            with self.subTest(status_code=status_code):
                def handler(request: httpx.Request) -> httpx.Response:
                    if request_target(request) == "/api/v1/groups/available":
                        return httpx.Response(200, json={"code": 0, "data": []})
                    if request.url.path == SUB2API_USAGE_STATS_ENDPOINT:
                        return httpx.Response(status_code)
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    access_token="sub2api-login-token",
                )
                self.assertIsNone(result.yesterday_balance_used)
                self.assertIsNone(result.yesterday_balance_unit)
                self.assertEqual(result.yesterday_balance_status, expected_status)

    def test_sub2api_today_key_usage_accepts_authoritative_zero(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"items": [{"id": 41, "key": "sk-zero-usage", "status": "active"}]},
                    },
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "41": {"api_key_id": 41, "today_actual_cost": 0}
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={12: "sk-zero-usage"},
        )

        self.assertEqual(result.account_upstream_states[12].usage_amount, 0)
        self.assertEqual(result.account_upstream_states[12].usage_unit, "USD")

    def test_sub2api_today_key_usage_batches_at_one_hundred_ids(self) -> None:
        account_api_keys = {
            account_id: f"sk-batch-{account_id:03d}"
            for account_id in range(1, 102)
        }
        records = [
            {"id": account_id, "key": api_key, "status": "active"}
            for account_id, api_key in account_api_keys.items()
        ]
        seen_batches: list[list[int]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": records}},
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                body = json.loads(request.content)
                batch = body["api_key_ids"]
                self.assertLessEqual(len(batch), 100)
                seen_batches.append(batch)
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                str(record_id): {
                                    "api_key_id": record_id,
                                    "today_actual_cost": record_id / 10,
                                }
                                for record_id in batch
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys=account_api_keys,
        )

        self.assertEqual(sorted(len(batch) for batch in seen_batches), [1, 100])
        self.assertEqual(result.account_upstream_states[1].usage_amount, 0.1)
        self.assertEqual(result.account_upstream_states[101].usage_amount, 10.1)

    def test_sub2api_exact_page_one_targets_skip_later_pages_and_key_reveal(self) -> None:
        target_key = "sk-page-one-target"
        later_page_requests: list[str] = []
        reveal_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [{"id": 41, "key": target_key, "status": "active"}],
                            "total": 601,
                            "page": 1,
                            "page_size": 200,
                            "pages": 4,
                        },
                    },
                )
            if target.startswith("/api/v1/keys?page="):
                later_page_requests.append(target)
                return httpx.Response(200, json={"code": 0, "data": {"items": []}})
            if target.startswith("/api/v1/keys/"):
                reveal_requests.append(target)
                return httpx.Response(200, json={"code": 0, "data": {"key": target_key}})
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "41": {"api_key_id": 41, "today_actual_cost": 3.25}
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={41: target_key},
        )

        self.assertEqual(result.account_upstream_states[41].usage_amount, 3.25)
        self.assertEqual(later_page_requests, [])
        self.assertEqual(reveal_requests, [])

    def test_cached_sub2api_masked_page_record_fails_closed_when_detail_fails(self) -> None:
        detail_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [{"id": 41, "key": "sk****rget", "status": "active"}],
                            "total": 1,
                        },
                    },
                )
            if target.startswith("/api/v1/keys/"):
                detail_requests.append(target)
                return httpx.Response(500)
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "41": {"api_key_id": 41, "today_actual_cost": 3.25}
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-page-one-target"},
            account_api_key_record_ids={7: 41},
        )

        self.assertNotIn(7, result.account_upstream_states)
        self.assertGreaterEqual(len(detail_requests), 1)
        self.assertEqual(set(detail_requests), {"/api/v1/keys/41"})

    def test_discovery_wrapper_forwards_cached_api_key_record_ids(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [{"id": 41, "key": "sk****rget", "status": "active"}],
                            "total": 1,
                        },
                    },
                )
            if target == "/api/v1/keys/41":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "id": 41,
                            "key": "sk-page-one-target",
                            "status": "active",
                        },
                    },
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"stats": {"41": {"today_actual_cost": 1.5}}}},
                )
            return httpx.Response(404)

        result = self.run_discovery_wrapper(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-page-one-target"},
            account_api_key_record_ids={7: 41},
        )

        self.assertEqual(result.account_upstream_states[7].key_record_id, 41)
        self.assertEqual(result.account_upstream_states[7].usage_amount, 1.5)

    def test_cached_record_id_without_local_key_remains_unverified(self) -> None:
        detail_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 41, "key": "sk-another-account", "status": "disabled"}
                            ],
                            "total": 1,
                        },
                    },
                )
            if target.startswith("/api/v1/keys/"):
                detail_requests.append(target)
                return httpx.Response(200, json={"code": 0, "data": {"id": 41}})
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_key_record_ids={7: 41},
        )

        self.assertNotIn(7, result.account_upstream_states)
        self.assertEqual(detail_requests, [])

    def test_cached_record_without_key_field_remains_unverified(self) -> None:
        detail_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [{"id": 41, "status": "disabled"}],
                            "total": 1,
                        },
                    },
                )
            if target == "/api/v1/keys/41":
                detail_requests.append(target)
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"id": 41, "status": "disabled"}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-local-seven"},
            account_api_key_record_ids={7: 41},
        )

        self.assertNotIn(7, result.account_upstream_states)
        self.assertGreaterEqual(detail_requests.count("/api/v1/keys/41"), 1)

    def test_cached_sub2api_key_record_id_fetches_detail_without_scanning_pages(self) -> None:
        later_page_requests: list[str] = []
        detail_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": [], "total": 601, "pages": 4}},
                )
            if target == "/api/v1/keys/401":
                detail_requests.append(target)
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "id": 401,
                            "key": "sk-record-on-later-page",
                            "status": "active",
                        },
                    },
                )
            if target.startswith("/api/v1/keys?page="):
                later_page_requests.append(target)
                return httpx.Response(500)
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"stats": {"401": {"today_actual_cost": 8.5}}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-record-on-later-page"},
            account_api_key_record_ids={7: 401},
        )

        self.assertEqual(result.account_upstream_states[7].key_record_id, 401)
        self.assertEqual(result.account_upstream_states[7].usage_amount, 8.5)
        self.assertEqual(detail_requests, ["/api/v1/keys/401"])
        self.assertEqual(later_page_requests, [])

    def test_cached_newapi_key_record_id_fetches_authenticated_detail(self) -> None:
        target_key = "sk-cached-newapi-target-Z7Q9"
        for detail_key in ("sk**********Z7Q9", target_key):
            with self.subTest(detail_key=detail_key):
                detail_requests: list[httpx.Request] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    target = request_target(request)
                    if target == "/api/user/self/groups":
                        return httpx.Response(200, json={"success": True, "data": {}})
                    if target == "/api/token/?p=1&page_size=200":
                        return httpx.Response(
                            200,
                            json={
                                "success": True,
                                "data": {
                                    "items": [
                                        {
                                            "id": 12,
                                            "key": "sk-unrelated-page-one-key",
                                            "status": 1,
                                        }
                                    ],
                                    "total": 1,
                                },
                            },
                        )
                    if target == "/api/token/401":
                        detail_requests.append(request)
                        return httpx.Response(
                            200,
                            json={
                                "success": True,
                                "data": {"id": 401, "key": detail_key, "status": 1},
                            },
                        )
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="newapi",
                    access_token="newapi-management-token",
                    new_api_user="17",
                    account_api_keys={7: target_key},
                    account_api_key_record_ids={7: 401},
                )

                if "*" in detail_key:
                    self.assertNotIn(7, result.account_upstream_states)
                else:
                    state = result.account_upstream_states[7]
                    self.assertEqual(state.key_record_id, 401)
                self.assertEqual(len(detail_requests), 1)
                detail_request = detail_requests[0]
                self.assertEqual(detail_request.method, "GET")
                self.assertEqual(request_target(detail_request), "/api/token/401")
                self.assertEqual(
                    detail_request.headers.get("Authorization"),
                    "Bearer newapi-management-token",
                )
                self.assertEqual(detail_request.headers.get("New-Api-User"), "17")

    def test_cached_newapi_masked_list_record_requires_full_detail_match(self) -> None:
        detail_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(200, json={"success": True, "data": {}})
            if target == "/api/token/?p=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": [{
                                "id": 401,
                                "key": "sk**********Z7Q9",
                                "status": 1,
                            }],
                            "total": 1,
                        },
                    },
                )
            if target == "/api/token/401":
                detail_requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "id": 401,
                            "key": "sk-another-account-with-Z7Q9",
                            "status": 1,
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            api_key="sk-cached-newapi-target-Z7Q9",
            access_token="newapi-management-token",
            new_api_user=17,
            account_api_keys={7: "sk-cached-newapi-target-Z7Q9"},
            account_api_key_record_ids={7: 401},
        )

        self.assertNotIn(7, result.account_upstream_states)
        self.assertIsNone(result.matched_account_state)
        self.assertEqual(
            [request_target(request) for request in detail_requests],
            ["/api/token/401"],
        )

    def test_cached_newapi_key_record_id_rejects_detail_for_another_key(self) -> None:
        detail_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(200, json={"success": True, "data": {}})
            if target == "/api/token/?p=1&page_size=200":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"items": [], "total": 0}},
                )
            if target == "/api/token/401":
                detail_requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "id": 401,
                            "key": "sk-another-newapi-account",
                            "status": 1,
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="newapi-management-token",
            new_api_user=17,
            account_api_keys={7: "sk-cached-newapi-target-Z7Q9"},
            account_api_key_record_ids={7: 401},
        )

        self.assertNotIn(7, result.account_upstream_states)
        self.assertEqual(
            [request_target(request) for request in detail_requests],
            ["/api/token/401"],
        )

    def test_missing_cached_sub2api_key_record_id_falls_back_and_rebinds(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [{"id": 52, "key": "sk-recreated-key", "status": "active"}],
                            "total": 1,
                        },
                    },
                )
            if target == "/api/v1/keys/41":
                return httpx.Response(404)
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"stats": {"52": {"today_actual_cost": 1.5}}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-recreated-key"},
            account_api_key_record_ids={7: 41},
        )

        self.assertEqual(result.account_upstream_states[7].key_record_id, 52)
        self.assertEqual(result.account_upstream_states[7].usage_amount, 1.5)

    def test_cached_record_id_for_another_key_is_rejected_and_rebound(self) -> None:
        detail_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 41, "key": "sk-another-account", "status": "disabled"},
                                {"id": 52, "key": "sk-current-account", "status": "active"},
                            ],
                            "total": 2,
                        },
                    },
                )
            if target == "/api/v1/keys/41":
                detail_requests.append(target)
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"id": 41, "key": "sk-another-account"}},
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"stats": {"52": {"today_actual_cost": 2.5}}}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-current-account"},
            account_api_key_record_ids={7: 41},
        )

        state = result.account_upstream_states[7]
        self.assertEqual(state.key_record_id, 52)
        self.assertEqual(state.key_status, "active")
        self.assertEqual(state.usage_amount, 2.5)
        self.assertEqual(detail_requests, ["/api/v1/keys/41"])

    def test_cached_record_ids_disambiguate_identically_masked_keys(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 11, "key": "sk****same", "status": "active"},
                                {"id": 12, "key": "sk****same", "status": "disabled"},
                            ],
                            "total": 2,
                        },
                    },
                )
            if target == "/api/v1/keys/11":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"id": 11, "key": "sk-first-same", "status": "active"}},
                )
            if target == "/api/v1/keys/12":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"id": 12, "key": "sk-other-same", "status": "disabled"}},
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "11": {"today_actual_cost": 1.1},
                                "12": {"today_actual_cost": 1.2},
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: "sk-first-same", 8: "sk-other-same"},
            account_api_key_record_ids={7: 11, 8: 12},
        )

        self.assertEqual(result.account_upstream_states[7].key_record_id, 11)
        self.assertEqual(result.account_upstream_states[7].key_status, "active")
        self.assertEqual(result.account_upstream_states[8].key_record_id, 12)
        self.assertEqual(result.account_upstream_states[8].key_status, "disabled")

    def test_key_usage_overlaps_reveal_and_monitor_detail_branches(self) -> None:
        async def scenario():
            direct_key = "sk-direct-account"
            unresolved_key = "sk-unresolved-secret"
            usage_started = asyncio.Event()
            reveal_observed_usage: list[bool] = []
            monitor_observed_usage: list[bool] = []
            usage_batches: list[list[int]] = []

            async def handler(request: httpx.Request) -> httpx.Response:
                target = request_target(request)
                if target == "/api/v1/groups/available":
                    return httpx.Response(200, json={"code": 0, "data": []})
                if target == "/api/v1/keys?page=1&page_size=200":
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "items": [
                                    {"id": 1, "key": direct_key, "status": "active"},
                                    {"id": 2, "key": "sk****cret", "status": "active"},
                                    {"id": 3, "key": "sk****cret", "status": "active"},
                                ],
                                "total": 3,
                                "page": 1,
                                "page_size": 200,
                                "pages": 1,
                            },
                        },
                    )
                if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                    return httpx.Response(
                        200,
                        json={"code": 0, "data": {"items": [{"id": 9, "name": "Primary"}]}},
                    )
                if request.url.path == f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/9/status":
                    await asyncio.wait_for(usage_started.wait(), timeout=0.5)
                    monitor_observed_usage.append(usage_started.is_set())
                    return httpx.Response(
                        200,
                        json={"code": 0, "data": {"primary_status": "operational"}},
                    )
                if target.startswith("/api/v1/keys/"):
                    await asyncio.wait_for(usage_started.wait(), timeout=0.5)
                    reveal_observed_usage.append(usage_started.is_set())
                    record_id = int(target.rsplit("/", 1)[-1])
                    revealed = unresolved_key if record_id == 2 else "sk-unrelated-secret"
                    return httpx.Response(200, json={"code": 0, "data": {"key": revealed}})
                if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                    batch = json.loads(request.content)["api_key_ids"]
                    usage_batches.append(batch)
                    if batch == [1]:
                        usage_started.set()
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "stats": {
                                    str(record_id): {
                                        "api_key_id": record_id,
                                        "today_actual_cost": float(record_id),
                                    }
                                    for record_id in batch
                                }
                            },
                        },
                    )
                return httpx.Response(404)

            client = UpstreamClient(
                transport=httpx.MockTransport(handler),
                resolver=public_resolver,
            )
            result = await client.discover(
                "https://upstream.example",
                upstream_type="sub2api",
                access_token="sub2api-login-token",
                account_api_keys={1: direct_key, 2: unresolved_key},
                include_channel_monitor_details=True,
            )
            return result, reveal_observed_usage, monitor_observed_usage, usage_batches

        result, reveal_observed, monitor_observed, usage_batches = asyncio.run(scenario())

        self.assertTrue(reveal_observed)
        self.assertTrue(all(reveal_observed))
        self.assertEqual(monitor_observed, [True])
        self.assertIn([1], usage_batches)
        self.assertIn([2], usage_batches)
        self.assertEqual(result.account_upstream_states[1].usage_amount, 1.0)
        self.assertEqual(result.account_upstream_states[2].usage_amount, 2.0)
        self.assertEqual(result.channel_monitors[0]["primary_status"], "operational")

    def test_sub2api_today_key_usage_reads_later_key_pages(self) -> None:
        target_key = "sk-page-two-target"
        seen_second_pages: list[httpx.Request] = []
        seen_usage_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": record_id,
                                    "key": f"sk-page-one-{record_id:03d}",
                                    "status": "active",
                                }
                                for record_id in range(1, 201)
                            ],
                            "total": 201,
                            "page": 1,
                            "page_size": 200,
                            "pages": 2,
                        },
                    },
                )
            if target == "/api/v1/keys?page=2&page_size=200":
                seen_second_pages.append(request)
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 201, "key": target_key, "status": "active"}
                            ],
                            "total": 201,
                            "page": 2,
                            "page_size": 200,
                            "pages": 2,
                        },
                    },
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                seen_usage_requests.append(request)
                self.assertEqual(json.loads(request.content), {"api_key_ids": [201]})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "201": {
                                    "api_key_id": 201,
                                    "today_actual_cost": 4.25,
                                }
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={201: target_key},
        )

        self.assertEqual(result.account_upstream_states[201].usage_amount, 4.25)
        self.assertEqual(len(seen_second_pages), 1)
        self.assertEqual(len(seen_usage_requests), 1)
        self.assertEqual(
            seen_second_pages[0].headers.get("Authorization"),
            "Bearer sub2api-login-token",
        )

    def test_later_page_mask_candidates_are_prioritized_for_key_reveal(self) -> None:
        target_key = "sk-sub2-target-secret-T9Z8"
        detail_ids: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": record_id,
                                    "key": f"sk****A{record_id:03d}",
                                    "status": "active",
                                }
                                for record_id in range(1, 201)
                            ],
                            "total": 202,
                            "page": 1,
                            "page_size": 200,
                            "pages": 2,
                        },
                    },
                )
            if target == "/api/v1/keys?page=2&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 201, "key": "sk****T9Z8", "status": "active"},
                                {"id": 202, "key": "sk****T9Z8", "status": "active"},
                            ],
                            "total": 202,
                            "page": 2,
                            "page_size": 200,
                            "pages": 2,
                        },
                    },
                )
            if target.startswith("/api/v1/keys/"):
                record_id = int(target.rsplit("/", 1)[-1])
                detail_ids.append(record_id)
                revealed_key = (
                    target_key
                    if record_id == 201
                    else f"sk-unrelated-detail-{record_id:03d}"
                )
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"key": revealed_key}},
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                self.assertEqual(json.loads(request.content), {"api_key_ids": [201]})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "201": {
                                    "api_key_id": 201,
                                    "today_actual_cost": 6.5,
                                }
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={7: target_key},
        )

        self.assertIn(201, detail_ids)
        self.assertIn(202, detail_ids)
        self.assertLessEqual(len(detail_ids), 200)
        self.assertEqual(result.account_upstream_states[7].usage_amount, 6.5)

    def test_key_reveal_budget_covers_rare_candidates_before_ambiguous_sets(self) -> None:
        ambiguous_key = "sk-high-ambiguity-target-AAAA"
        rare_key = "sk-rare-target-BBBB"
        detail_ids: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": record_id, "key": "sk****AAAA", "status": "active"}
                                for record_id in range(1, 201)
                            ],
                            "total": 202,
                            "page": 1,
                            "page_size": 200,
                            "pages": 2,
                        },
                    },
                )
            if target == "/api/v1/keys?page=2&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 201, "key": "sk****BBBB", "status": "active"},
                                {"id": 202, "key": "sk****BBBB", "status": "active"},
                            ],
                            "total": 202,
                            "page": 2,
                            "page_size": 200,
                            "pages": 2,
                        },
                    },
                )
            if target.startswith("/api/v1/keys/"):
                record_id = int(target.rsplit("/", 1)[-1])
                detail_ids.append(record_id)
                revealed_key = rare_key if record_id == 201 else f"sk-unrelated-{record_id}"
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"key": revealed_key}},
                )
            if request.url.path == SUB2API_API_KEY_USAGE_ENDPOINT:
                self.assertEqual(json.loads(request.content), {"api_key_ids": [201]})
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "stats": {
                                "201": {
                                    "api_key_id": 201,
                                    "today_actual_cost": 7.75,
                                }
                            }
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            account_api_keys={1: ambiguous_key, 2: rare_key},
        )

        self.assertIn(201, detail_ids)
        self.assertIn(202, detail_ids)
        self.assertLessEqual(len(detail_ids), 200)
        self.assertNotIn(1, result.account_upstream_states)
        self.assertEqual(result.account_upstream_states[2].usage_amount, 7.75)

    def test_sub2api_channel_monitor_details_are_opt_in(self) -> None:
        detail_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request_target(request) == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": 17,
                                    "name": "Summary",
                                    "primary_status": "degraded",
                                }
                            ]
                        },
                    },
                )
            if request.url.path == f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/17/status":
                detail_requests.append(request)
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"primary_status": "operational"}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
        )

        self.assertEqual(detail_requests, [])
        self.assertEqual(result.channel_monitors[0]["primary_status"], "degraded")

    def test_sub2api_channel_monitor_list_can_be_skipped(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            return httpx.Response(404)

        self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            optimized_endpoint_fallbacks=True,
            include_channel_monitors=False,
        )

        self.assertNotIn(SUB2API_CHANNEL_MONITORS_ENDPOINT, requested_paths)

    def test_sub2api_monitor_only_discovery_skips_balance_usage_and_key_endpoints(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": 17,
                                    "name": "Summary",
                                    "primary_status": "ok",
                                }
                            ]
                        },
                    },
                )
            if request.url.path == f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/17/status":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"primary_status": "success"}},
                )
            self.fail(f"monitor-only discovery requested unrelated endpoint {request.url.path}")

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            include_channel_monitor_details=True,
            monitor_only=True,
        )

        self.assertEqual(
            requested_paths,
            [
                SUB2API_CHANNEL_MONITORS_ENDPOINT,
                f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/17/status",
            ],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.channel_monitors_status, "ok")
        self.assertEqual(result.channel_monitors[0]["primary_status"], "available")

    def test_newapi_monitor_only_reads_public_uptime_panel_without_credentials(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("New-Api-User", request.headers)
            if request.url.path == NEWAPI_UPTIME_STATUS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": [
                            {
                                "categoryName": "Models",
                                "monitors": [
                                    {
                                        "name": "OpenAI",
                                        "group": "Primary",
                                        "status": 1,
                                        "uptime": 0.9985,
                                    },
                                    {
                                        "name": "Claude",
                                        "group": "Fallback",
                                        "status": 0,
                                        "uptime": 0.75,
                                    },
                                ],
                            }
                        ],
                    },
                )
            self.fail(f"monitor-only discovery requested unrelated endpoint {request.url.path}")

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="must-not-be-sent",
            new_api_user="7",
            monitor_only=True,
        )

        self.assertEqual(requested_paths, [NEWAPI_UPTIME_STATUS_ENDPOINT])
        self.assertTrue(result.ok)
        self.assertEqual(result.channel_monitors_status, "ok")
        self.assertEqual(result.channel_monitors_total, 2)
        self.assertEqual(
            [item["primary_status"] for item in result.channel_monitors],
            ["available", "unavailable"],
        )
        self.assertEqual(result.channel_monitors[0]["provider"], "uptime-kuma")
        self.assertEqual(result.channel_monitors[0]["group_name"], "Models · Primary")
        self.assertEqual(result.channel_monitors[0]["availability_7d"], 0.9985)
        self.assertEqual(result.channel_monitors[0]["availability_window"], "24h")
        self.assertLessEqual(result.channel_monitors[0]["id"], (1 << 53) - 1)

    def test_public_uptime_alone_does_not_make_regular_newapi_discovery_succeed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == NEWAPI_UPTIME_STATUS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": [
                            {
                                "categoryName": "Models",
                                "monitors": [
                                    {"name": "OpenAI", "status": 1, "uptime": 1.0}
                                ],
                            }
                        ],
                    },
                )
            if request.url.path == "/api/status":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"price": 0.1}},
                )
            if request.url.path == "/api/pricing":
                return httpx.Response(200, json={"success": True, "data": []})
            return httpx.Response(401)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            access_token="expired-token",
            new_api_user="7",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.channel_monitors_status, "unknown")

    def test_auto_monitor_only_prefers_successful_newapi_uptime_over_sub2api_401(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == NEWAPI_UPTIME_STATUS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": [
                            {
                                "categoryName": "Models",
                                "monitors": [
                                    {"name": "OpenAI", "status": 1, "uptime": 1.0}
                                ],
                            }
                        ],
                    },
                )
            if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                return httpx.Response(401)
            self.fail(f"monitor-only discovery requested unrelated endpoint {request.url.path}")

        result = self.run_discovery(
            handler,
            upstream_type="auto",
            access_token="sub2api-token",
            monitor_only=True,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.upstream_type, "newapi")
        self.assertEqual(result.channel_monitors_status, "ok")
        self.assertFalse(result.sub2api_auth_rejected)

    def test_auto_monitor_only_preserves_unresolved_sub2api_credential_rejection(self) -> None:
        for rejected_status in (401, 403):
            with self.subTest(rejected_status=rejected_status):
                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == NEWAPI_UPTIME_STATUS_ENDPOINT:
                        return httpx.Response(404)
                    if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                        return httpx.Response(rejected_status)
                    self.fail(
                        f"monitor-only discovery requested unrelated endpoint {request.url.path}"
                    )

                result = self.run_discovery(
                    handler,
                    upstream_type="auto",
                    access_token="rejected-sub2api-token",
                    monitor_only=True,
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.upstream_type, "sub2api")
                self.assertTrue(result.sub2api_auth_rejected)
                self.assertEqual(
                    result.channel_monitors_status,
                    "credentials_rejected",
                )

    def test_monitor_only_preserves_unsupported_and_rejected_statuses(self) -> None:
        newapi_result = self.run_discovery(
            lambda _request: httpx.Response(404),
            upstream_type="newapi",
            monitor_only=True,
        )
        self.assertTrue(newapi_result.ok)
        self.assertEqual(newapi_result.channel_monitors_status, "unsupported")

        sub2api_result = self.run_discovery(
            lambda _request: httpx.Response(403),
            upstream_type="sub2api",
            access_token="rejected-token",
            monitor_only=True,
        )
        self.assertFalse(sub2api_result.ok)
        self.assertTrue(sub2api_result.sub2api_auth_rejected)
        self.assertEqual(
            sub2api_result.channel_monitors_status,
            "credentials_rejected",
        )

    def test_newapi_empty_public_uptime_panel_is_not_configured(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, NEWAPI_UPTIME_STATUS_ENDPOINT)
            return httpx.Response(200, json={"success": True, "data": []})

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            monitor_only=True,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.channel_monitors, [])
        self.assertEqual(result.channel_monitors_status, "not_configured")
        self.assertIn("no public uptime monitors", result.channel_monitors_message)

    def test_newapi_configured_empty_uptime_group_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, NEWAPI_UPTIME_STATUS_ENDPOINT)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"categoryName": "Models", "monitors": []}],
                },
            )

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            monitor_only=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.channel_monitors, [])
        self.assertEqual(result.channel_monitors_status, "error")
        self.assertIn("configured", result.channel_monitors_message)

    def test_newapi_partial_empty_uptime_group_preserves_error_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, NEWAPI_UPTIME_STATUS_ENDPOINT)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {"categoryName": "Failed", "monitors": []},
                        {
                            "categoryName": "Healthy",
                            "monitors": [
                                {"name": "OpenAI", "status": 1, "uptime": 1.0}
                            ],
                        },
                    ],
                },
            )

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            monitor_only=True,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.channel_monitors_status, "error")
        self.assertEqual(len(result.channel_monitors), 1)
        self.assertIn("incomplete data", result.channel_monitors_message)

    def test_newapi_duplicate_uptime_monitors_merge_with_stable_id(self) -> None:
        duplicate_monitors = [
            {"name": "OpenAI", "group": "Primary", "status": 1, "uptime": 0.99},
            {"name": "OpenAI", "group": "Primary", "status": 0, "uptime": 0.75},
        ]

        def discover(monitors: list[dict[str, object]]) -> DiscoveryResult:
            return self.run_discovery(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": [{"categoryName": "Models", "monitors": monitors}],
                    },
                ),
                upstream_type="newapi",
                monitor_only=True,
            )

        forward = discover(duplicate_monitors)
        reversed_result = discover(list(reversed(duplicate_monitors)))

        self.assertTrue(forward.ok)
        self.assertEqual(forward.channel_monitors_total, 1)
        self.assertEqual(len(forward.channel_monitors), 1)
        self.assertEqual(
            forward.channel_monitors[0]["id"],
            reversed_result.channel_monitors[0]["id"],
        )
        self.assertEqual(forward.channel_monitors[0]["primary_status"], "unavailable")
        self.assertEqual(reversed_result.channel_monitors[0]["primary_status"], "unavailable")
        self.assertEqual(forward.channel_monitors[0]["availability_7d"], 0.75)
        self.assertEqual(reversed_result.channel_monitors[0]["availability_7d"], 0.75)

    def test_channel_monitor_available_status_aliases_are_normalized(self) -> None:
        for status in ("ok", "success", "active", "enabled"):
            with self.subTest(status=status):
                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": {
                                    "items": [
                                        {
                                            "id": 17,
                                            "name": "Summary",
                                            "primary_status": status,
                                        }
                                    ]
                                },
                            },
                        )
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    access_token="sub2api-login-token",
                    monitor_only=True,
                )
                self.assertEqual(
                    result.channel_monitors[0]["primary_status"],
                    "available",
                )

    def test_monitor_only_401_marks_sub2api_auth_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, SUB2API_CHANNEL_MONITORS_ENDPOINT)
            return httpx.Response(401)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="expired-access-token",
            include_channel_monitor_details=True,
            monitor_only=True,
        )

        self.assertEqual(result.status, "error")
        self.assertTrue(result.sub2api_auth_rejected)
        self.assertEqual(result.channel_monitors_status, "credentials_rejected")

    def test_sub2api_channel_monitors_merge_summary_and_status_details(self) -> None:
        secret = "monitor-detail-secret"
        detail_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request_target(request) == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "id": 17,
                                    "name": "Summary name",
                                    "provider": "openai",
                                    "group_name": "Primary",
                                    "primary_model": "gpt-summary",
                                    "primary_status": "degraded",
                                    "primary_latency_ms": 999,
                                },
                                {
                                    "id": 18,
                                    "name": "Fallback summary",
                                    "provider": "grok",
                                    "group_name": "Backup",
                                    "primary_model": "grok-summary",
                                    "primary_status": "operational",
                                    "primary_latency_ms": 88,
                                },
                            ]
                        },
                    },
                )
            if request.url.path == f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/17/status":
                detail_requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "monitor": {
                                "name": "Detail must not replace the summary",
                                "provider": "injected-provider",
                                "primary_status": "available",
                                "availability7d": "99.75",
                                "api_key": secret,
                            },
                            "result": {"primaryPingLatencyMs": 12.5},
                            "status": {
                                "primaryLatencyMs": 34.5,
                                "extraModels": [
                                    {
                                        "name": f"gpt-detail-{secret}",
                                        "status": "healthy",
                                        "latencyMs": 45,
                                    }
                                ],
                                "timeline": [
                                    {
                                        "time": "2026-07-19T12:30:00Z",
                                        "status": "available",
                                        "latencyMs": 36,
                                        "pingLatencyMs": 13,
                                    }
                                ],
                            },
                        },
                    },
                )
            if request.url.path == f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/18/status":
                detail_requests.append(request)
                return httpx.Response(503)
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token=secret,
            include_channel_monitor_details=True,
        )

        self.assertEqual(result.channel_monitors_status, "ok")
        self.assertEqual(result.channel_monitors_total, 2)
        self.assertEqual(len(detail_requests), 2)
        self.assertEqual(
            {request.url.path for request in detail_requests},
            {
                f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/17/status",
                f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/18/status",
            },
        )
        self.assertTrue(all(request.method == "GET" for request in detail_requests))
        self.assertTrue(
            all(
                request.headers.get("Authorization") == f"Bearer {secret}"
                for request in detail_requests
            )
        )

        detailed, fallback = result.channel_monitors
        self.assertEqual(detailed["name"], "Summary name")
        self.assertEqual(detailed["provider"], "openai")
        self.assertEqual(detailed["group_name"], "Primary")
        self.assertEqual(detailed["primary_model"], "gpt-summary")
        self.assertEqual(detailed["primary_status"], "available")
        self.assertEqual(detailed["primary_latency_ms"], 34.5)
        self.assertEqual(detailed["primary_ping_latency_ms"], 12.5)
        self.assertEqual(detailed["availability_7d"], 99.75)
        self.assertEqual(detailed["extra_models"][0]["model"], "gpt-detail-[redacted]")
        self.assertEqual(detailed["timeline"][0]["checked_at"], "2026-07-19T12:30:00Z")
        self.assertEqual(fallback["name"], "Fallback summary")
        self.assertEqual(fallback["primary_status"], "operational")
        self.assertEqual(fallback["primary_latency_ms"], 88)
        self.assertIn("Used list summaries for 1 monitor(s)", result.channel_monitors_message)
        self.assertNotIn(secret, json.dumps(result.as_dict()))

    def test_sub2api_channel_monitor_details_can_target_one_monitor(self) -> None:
        detail_ids: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 17, "name": "First", "primary_status": "degraded"},
                                {"id": 18, "name": "Second", "primary_status": "degraded"},
                            ]
                        },
                    },
                )
            if (
                request.url.path.startswith(f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/")
                and request.url.path.endswith("/status")
            ):
                monitor_id = int(request.url.path.rsplit("/", 2)[-2])
                detail_ids.append(monitor_id)
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"primary_status": "operational"}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            include_channel_monitor_details=True,
            channel_monitor_detail_ids={18},
            monitor_only=True,
        )

        self.assertEqual(detail_ids, [18])
        self.assertEqual(
            [monitor["primary_status"] for monitor in result.channel_monitors],
            ["degraded", "operational"],
        )

    def test_sub2api_channel_monitor_detail_concurrency_is_bounded(self) -> None:
        active_details = 0
        max_active_details = 0
        detail_count = CHANNEL_MONITOR_DETAIL_CONCURRENCY + 5

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active_details, max_active_details
            if request_target(request) == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": monitor_id, "name": f"Monitor {monitor_id}"}
                                for monitor_id in range(1, detail_count + 1)
                            ]
                        },
                    },
                )
            if (
                request.url.path.startswith(f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/")
                and request.url.path.endswith("/status")
            ):
                active_details += 1
                max_active_details = max(max_active_details, active_details)
                await asyncio.sleep(0.005)
                active_details -= 1
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"primary_status": "operational"}},
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token="sub2api-login-token",
            include_channel_monitor_details=True,
        )

        self.assertEqual(len(result.channel_monitors), detail_count)
        self.assertGreater(max_active_details, 1)
        self.assertLessEqual(max_active_details, CHANNEL_MONITOR_DETAIL_CONCURRENCY)

    def test_sub2api_channel_monitors_are_bounded_cleaned_and_scrubbed(self) -> None:
        secret = "monitor-secret-token-" + ("S" * 220)
        detail_ids: list[int] = []
        extras = [
            {"model": None},
            *(
                {
                    "model": f"model-{index}-{secret if index == 0 else 'safe'}",
                    "status": "operational",
                    "latency_ms": index,
                }
                for index in range(MAX_CHANNEL_MONITOR_EXTRA_MODELS + 5)
            ),
        ]
        timeline = [
            {"checked_at": "not-a-timestamp"},
            *(
                {
                    "status": "degraded",
                    "latency_ms": index,
                    "ping_latency_ms": index + 1,
                    "checked_at": f"2026-07-18T00:{index % 60:02d}:00Z",
                }
                for index in range(MAX_CHANNEL_MONITOR_TIMELINE_POINTS + 5)
            ),
        ]
        monitors = [
            {"id": 0, "name": "invalid"},
            {
                "id": 1,
                "name": f"Status {secret}",
                "provider": "OpenAI injected value",
                "group_name": "Primary",
                "primary_model": "gpt-test",
                "primary_status": "compromised",
                "primary_latency_ms": -1,
                "primary_ping_latency_ms": "12.5",
                "availability_7d": 101,
                "extra_models": extras,
                "timeline": timeline,
                "api_key": secret,
            },
            *(
                {
                    "id": index,
                    "name": f"Monitor {index}",
                    "provider": "grok",
                    "primary_model": "grok-test",
                    "primary_status": "operational",
                    "availability_7d": 99.5,
                }
                for index in range(2, MAX_CHANNEL_MONITORS + 6)
            ),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request_target(request) == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": monitors}},
                )
            if (
                request.url.path.startswith(f"{SUB2API_CHANNEL_MONITORS_ENDPOINT}/")
                and request.url.path.endswith("/status")
            ):
                monitor_id = int(request.url.path.rsplit("/", 2)[-2])
                detail_ids.append(monitor_id)
                return httpx.Response(404)
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            access_token=secret,
            include_channel_monitor_details=True,
        )

        self.assertEqual(result.channel_monitors_status, "ok")
        self.assertEqual(len(result.channel_monitors), MAX_CHANNEL_MONITORS)
        self.assertEqual(result.channel_monitors_total, MAX_CHANNEL_MONITORS + 5)
        self.assertEqual(len(detail_ids), MAX_CHANNEL_MONITORS)
        self.assertEqual(set(detail_ids), set(range(1, MAX_CHANNEL_MONITORS + 1)))
        first = result.channel_monitors[0]
        self.assertEqual(first["name"], "Status [redacted]")
        self.assertEqual(first["provider"], "unknown")
        self.assertEqual(first["primary_status"], "unknown")
        self.assertIsNone(first["primary_latency_ms"])
        self.assertEqual(first["primary_ping_latency_ms"], 12.5)
        self.assertIsNone(first["availability_7d"])
        self.assertEqual(len(first["extra_models"]), MAX_CHANNEL_MONITOR_EXTRA_MODELS)
        self.assertEqual(len(first["timeline"]), MAX_CHANNEL_MONITOR_TIMELINE_POINTS)
        serialized = json.dumps(result.as_dict())
        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret[:160], serialized)
        self.assertNotIn('"api_key"', serialized)

    def test_sub2api_channel_monitor_failures_are_explicit(self) -> None:
        for status_code, expected_status in (
            (401, "credentials_rejected"),
            (404, "unsupported"),
            (405, "unsupported"),
            (500, "error"),
        ):
            with self.subTest(status_code=status_code):
                def handler(request: httpx.Request) -> httpx.Response:
                    if request_target(request) == "/api/v1/groups/available":
                        return httpx.Response(200, json={"code": 0, "data": []})
                    if request.url.path == SUB2API_CHANNEL_MONITORS_ENDPOINT:
                        return httpx.Response(status_code)
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    access_token="sub2api-login-token",
                )
                self.assertEqual(result.channel_monitors, [])
                self.assertEqual(result.channel_monitors_status, expected_status)

    def test_channel_monitor_scrubbing_tolerates_non_list_collections(self) -> None:
        result = _scrub_discovery_result(
            DiscoveryResult(
                upstream_type="sub2api",
                source="https://upstream.example",
                status="ok",
                channel_monitors=[
                    {
                        "id": 1,
                        "name": "Monitor",
                        "extra_models": "not-a-list",
                        "timeline": {"status": "operational"},
                    }
                ],
            ),
            ("secret",),
        )

        self.assertEqual(result.channel_monitors[0]["extra_models"], [])
        self.assertEqual(result.channel_monitors[0]["timeline"], [])

    def test_channel_monitor_scrubbing_reapplies_a_strict_field_allowlist(self) -> None:
        secret = "manually-constructed-monitor-secret"
        result = _scrub_discovery_result(
            DiscoveryResult(
                upstream_type="sub2api",
                source="https://upstream.example",
                status="ok",
                channel_monitors=[
                    {
                        "id": 1,
                        "name": "Monitor",
                        "raw_secret": secret,
                        "extra_models": [
                            {
                                "model": "model-a",
                                "status": "operational",
                                "latency_ms": 1,
                                "api_key": secret,
                            }
                        ],
                        "timeline": [
                            {
                                "status": "operational",
                                "latency_ms": 1,
                                "ping_latency_ms": 2,
                                "checked_at": "2026-07-18T00:00:00Z",
                                "raw_secret": secret,
                            }
                        ],
                    }
                ],
            ),
            (secret,),
        )

        monitor = result.channel_monitors[0]
        self.assertNotIn("raw_secret", monitor)
        self.assertNotIn("api_key", monitor["extra_models"][0])
        self.assertNotIn("raw_secret", monitor["timeline"][0])
        self.assertNotIn(secret, json.dumps(result.as_dict()))

    def test_sub2api_orphaned_key_group_is_deleted_even_without_a_rate(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(200, json={"code": 0, "data": []})
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "key": "sk-orphaned-key",
                                    "status": "active",
                                    "group_id": 99,
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            account_api_keys={8: "sk-orphaned-key"},
        )

        state = result.account_upstream_states[8]
        self.assertEqual(state.key_status, "active")
        self.assertEqual(state.group_status, "deleted")
        self.assertEqual(state.group_id, "99")
        self.assertNotIn(8, result.account_group_matches)

    def test_failed_group_endpoint_leaves_group_state_unknown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
                return httpx.Response(503)
            if target == "/api/v1/keys?page=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "key": "sk-active-key",
                                    "status": "active",
                                    "group_id": 2,
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="sub2api",
            account_api_keys={9: "sk-active-key"},
        )

        state = result.account_upstream_states[9]
        self.assertEqual(state.key_status, "active")
        self.assertIsNone(state.group_status)

    def test_newapi_numeric_disabled_status_is_normalized(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/user/self/groups":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"default": {"ratio": 1}}},
                )
            if target == "/api/token/?p=1&page_size=200":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": [
                                {
                                    "key": "newapi-disabled-key",
                                    "status": 2,
                                    "group": "default",
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(404)

        result = self.run_discovery(
            handler,
            upstream_type="newapi",
            account_api_keys={10: "newapi-disabled-key"},
        )

        state = result.account_upstream_states[10]
        self.assertEqual(state.key_status, "disabled")
        self.assertEqual(state.group_status, "available")


if __name__ == "__main__":
    unittest.main()
