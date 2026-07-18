from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from app.services.upstream_client import (
    MAX_DOH_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_UPSTREAM_TOKEN_LENGTH,
    NEWAPI_ENDPOINTS,
    NEWAPI_TODAY_USAGE_ENDPOINT,
    SUB2API_REFRESH_ENDPOINT,
    SUB2API_TODAY_USAGE_ENDPOINT,
    UpstreamClient,
    _default_resolver,
    _doh_resolver,
    _invalidate_dns_cache,
    _newapi_today_usage_params,
    _unique_masked_api_key_records,
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
        self.assertEqual(len(seen), 7)
        self.assertNotIn("/api/token/search?p=1&size=200", seen)
        self.assertNotIn("/api/v1/payment/config", seen)
        self.assertEqual(seen.count("/api/status"), 1)

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
                if headers[0] != "/api/status"
            )
        )

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
        self.assertEqual(set(result.account_group_matches), {7, 8, 10})
        self.assertEqual(result.account_group_matches[7].name, "default")
        self.assertEqual(result.account_group_matches[7].multiplier, 1)
        self.assertEqual(result.account_group_matches[8].name, "vip")
        self.assertEqual(result.account_group_matches[8].multiplier, 2)
        self.assertEqual(result.account_group_matches[10].name, "retired")
        self.assertIsNone(result.account_group_matches[10].multiplier)
        targets = [request_target(request) for request in seen]
        for endpoint in NEWAPI_ENDPOINTS:
            if endpoint == NEWAPI_TODAY_USAGE_ENDPOINT:
                self.assertEqual(
                    sum(request.url.path == endpoint for request in seen),
                    1,
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
        seen_today_request: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if request.url.path == NEWAPI_TODAY_USAGE_ENDPOINT:
                seen_today_request.append(request)
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"quota": 2_500_000}},
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
        self.assertEqual(len(seen_today_request), 1)
        request = seen_today_request[0]
        self.assertEqual(request.headers.get("Authorization"), "console-access-token")
        self.assertEqual(request.headers.get("New-Api-User"), "42")
        start_timestamp = int(request.url.params["start_timestamp"])
        end_timestamp = int(request.url.params["end_timestamp"])
        zone = ZoneInfo("America/New_York")
        start = datetime.fromtimestamp(start_timestamp, zone)
        end = datetime.fromtimestamp(end_timestamp, zone)
        self.assertEqual((start.hour, start.minute, start.second), (0, 0, 0))
        self.assertEqual(start.date(), end.date())
        self.assertGreaterEqual(end_timestamp, start_timestamp)

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

    def test_sub2api_unique_mask_keeps_group_fields_without_reveal(self) -> None:
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
        self.assertEqual(reveal_calls, 0)
        self.assertEqual(result.account_group_matches[7].id, "2")
        self.assertEqual(result.account_group_matches[7].name, "premium")
        self.assertEqual(result.account_group_matches[7].multiplier, 2.75)
        self.assertNotIn(account_key, json.dumps(result.as_dict()))

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

        def handler(request: httpx.Request) -> httpx.Response:
            target = request_target(request)
            if target == "/api/v1/groups/available":
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
                                    "quota_used": 12.375,
                                }
                            ]
                        },
                    },
                )
            if target == SUB2API_TODAY_USAGE_ENDPOINT:
                seen_today_headers.append(
                    (
                        request.headers.get("Authorization"),
                        request.headers.get("New-Api-User"),
                    )
                )
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"today_actual_cost": 3.25}},
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
        self.assertEqual(seen_today_headers, [("Bearer sub2api-login-token", None)])

    def test_usage_amount_rejects_invalid_values(self) -> None:
        cases = (-1, "nan", True, None)
        for quota_used in cases:
            with self.subTest(quota_used=quota_used):
                def handler(request: httpx.Request) -> httpx.Response:
                    target = request_target(request)
                    if target == "/api/v1/groups/available":
                        return httpx.Response(200, json={"code": 0, "data": []})
                    if target == "/api/v1/keys?page=1&page_size=200":
                        record = {"key": "sk-invalid-usage", "status": "active"}
                        if quota_used is not None:
                            record["quota_used"] = quota_used
                        return httpx.Response(
                            200,
                            json={"code": 0, "data": {"items": [record]}},
                        )
                    return httpx.Response(404)

                result = self.run_discovery(
                    handler,
                    upstream_type="sub2api",
                    account_api_keys={11: "sk-invalid-usage"},
                )
                self.assertIsNone(result.account_upstream_states[11].usage_amount)

    def test_sub2api_orphaned_key_group_is_unavailable_even_without_a_rate(self) -> None:
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
        self.assertEqual(state.group_status, "unavailable")
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
