from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.schemas import AccountEditConfiguration
from app.services.account_editor import (
    AccountEditorResources,
    account_configuration_from_remote,
    account_extra_patch_from_configuration,
    account_identity_fingerprint,
    load_account_editor_resources,
    validate_account_edit_configuration,
)
from app.services.sub2api import Sub2ApiClient


def remote_account(**overrides: object) -> dict:
    account = {
        "id": 17,
        "name": "oauth-17",
        "platform": "openai",
        "type": "oauth",
        "email": "oauth-17@example.test",
        "concurrency": 2,
        "priority": 1,
        "rate_multiplier": 1.0,
        "status": "active",
        "schedulable": True,
        "proxy_id": 9,
        "group_ids": [3],
        "credentials": {
            "access_token": "must-not-leak",
            "model_mapping": {"gpt-5.4": "gpt-5.4"},
        },
        "extra": {},
    }
    account.update(overrides)
    return account


class FakeEditorSub2Api(Sub2ApiClient):
    def __init__(self) -> None:
        super().__init__()
        self.account = remote_account()
        self.groups = [
            {"id": 9, "name": "Second by name", "status": "active", "rate_multiplier": 1.0},
            {"id": 3, "name": "First by name", "status": "active", "rate_multiplier": 1.0},
        ]
        self.proxies = [{"id": 9, "name": "Proxy 9", "status": "active"}]
        self.models = [
            {"id": "gpt-5.4", "display_name": "GPT-5.4"},
            {"id": "gpt-5.6", "display_name": "GPT-5.6"},
        ]

    async def get_account_by_id(self, account_id: str | int, **_kwargs) -> dict | None:
        return self.account if int(account_id) == 17 else None

    async def list_groups_for_platform(self, platform: str | None, **_kwargs) -> list[dict]:
        self.assert_platform = platform
        return list(self.groups)

    async def list_proxies(self) -> list[dict]:
        return list(self.proxies)

    async def list_account_model_candidates(self, platform: str) -> list[dict[str, str]]:
        self.assert_model_platform = platform
        return list(self.models)


class AccountEditorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_live_resources_and_extracts_only_editable_configuration(self) -> None:
        sub2api = FakeEditorSub2Api()

        resources = await load_account_editor_resources(sub2api, 17)
        configuration = account_configuration_from_remote(sub2api, resources.account)

        self.assertEqual(sub2api.assert_platform, "openai")
        self.assertEqual(sub2api.assert_model_platform, "openai")
        self.assertEqual([group["id"] for group in resources.groups], [9, 3])
        self.assertEqual(configuration.proxy_id, 9)
        self.assertEqual(configuration.group_ids, [3])
        self.assertEqual(configuration.model_whitelist, ["gpt-5.4"])
        self.assertEqual(configuration.status, "active")
        self.assertEqual(configuration.openai_ws_mode, "off")
        self.assertEqual(configuration.codex_image_tool_mode, "inherit")
        self.assertEqual(configuration.openai_compact_mode, "auto")
        self.assertEqual(configuration.auto_pause_5h_threshold_percent, 0)
        self.assertNotIn("access_token", configuration.model_dump())
        self.assertEqual(len(account_identity_fingerprint(sub2api, resources.account)), 64)

    async def test_deleted_template_resources_are_rejected_before_update(self) -> None:
        sub2api = FakeEditorSub2Api()
        resources = await load_account_editor_resources(sub2api, 17)
        resources = AccountEditorResources(
            account=resources.account,
            groups=[],
            proxies=[],
            model_candidates=resources.model_candidates,
            model_candidates_complete=True,
            checked_at=resources.checked_at,
        )
        configuration = AccountEditConfiguration(
            concurrency=4,
            priority=2,
            rate_multiplier=1.25,
            schedulable=True,
            proxy_id=9,
            group_ids=[3],
            model_whitelist=["gpt-5.4"],
        )

        with self.assertRaisesRegex(ValueError, "模板.*分组 #3.*代理 #9"):
            validate_account_edit_configuration(configuration, resources, preset_name="工作日")

    async def test_removed_model_is_reported_from_complete_candidate_catalog(self) -> None:
        sub2api = FakeEditorSub2Api()
        resources = await load_account_editor_resources(sub2api, 17)
        configuration = AccountEditConfiguration(
            concurrency=2,
            priority=1,
            rate_multiplier=1,
            schedulable=True,
            proxy_id=None,
            group_ids=[],
            model_whitelist=["gpt-removed"],
        )

        with self.assertRaisesRegex(ValueError, "gpt-removed.*不可用"):
            validate_account_edit_configuration(configuration, resources, preset_name="旧模型")


class AccountEditorSub2ApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_update_uses_one_put_and_confirms_readback(self) -> None:
        state = remote_account(notes="old note")
        requests: list[tuple[str, str, dict | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            requests.append((request.method, request.url.path, body))
            if request.method == "PUT":
                assert body is not None
                state["notes"] = body["notes"]
                return httpx.Response(200, json={"data": state})
            return httpx.Response(200, json={"data": state})

        config = SimpleNamespace(
            base_url="http://sub2api.test/api/v1",
            auth_token="test-token",
            auth_header="X-API-Key",
            auth_scheme="",
            accounts_path="/admin/accounts",
        )
        runtime_config = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=config))
        client = Sub2ApiClient(transport=httpx.MockTransport(handler))

        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime_config):
            updated = await client.update_account_notes(17, "new synced note")

        self.assertEqual(client.account_notes(updated), "new synced note")
        self.assertEqual([item[:2] for item in requests], [
            ("GET", "/api/v1/admin/accounts/17"),
            ("PUT", "/api/v1/admin/accounts/17"),
            ("GET", "/api/v1/admin/accounts/17"),
        ])
        self.assertEqual(requests[1][2], {"notes": "new synced note"})

    async def test_bulk_update_merges_model_whitelist_and_confirms_readback(self) -> None:
        state = remote_account()
        requests: list[tuple[str, str, dict | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            requests.append((request.method, request.url.path, body))
            if request.method == "POST":
                assert body is not None
                state.update(
                    name=body["name"],
                    concurrency=body["concurrency"],
                    priority=body["priority"],
                    rate_multiplier=body["rate_multiplier"],
                    status=body["status"],
                    schedulable=body["schedulable"],
                    proxy_id=body["proxy_id"] or None,
                    group_ids=body["group_ids"],
                )
                state["credentials"] = {
                    **state["credentials"],
                    **body["credentials"],
                }
                state["extra"] = {**state.get("extra", {}), **body.get("extra", {})}
                return httpx.Response(200, json={"data": {"success": 1, "failed_ids": []}})
            return httpx.Response(200, json={"data": state})

        config = SimpleNamespace(
            base_url="http://sub2api.test/api/v1",
            auth_token="test-token",
            auth_header="X-API-Key",
            auth_scheme="",
            accounts_path="/admin/accounts",
        )
        runtime_config = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=config))
        client = Sub2ApiClient(transport=httpx.MockTransport(handler))

        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime_config):
            updated = await client.update_account_configuration(
                17,
                name="renamed",
                concurrency=5,
                priority=3,
                rate_multiplier=1.5,
                status="active",
                schedulable=False,
                proxy_id=None,
                group_ids=[3],
                model_whitelist=["gpt-5.4", "gpt-5.6"],
                extra_patch={
                    "openai_oauth_responses_websockets_v2_mode": "http_bridge",
                    "openai_oauth_responses_websockets_v2_enabled": True,
                    "codex_image_generation_bridge": True,
                    "codex_image_generation_explicit_tool_policy": None,
                    "auto_pause_5h_threshold": 0.8,
                },
            )

        self.assertEqual(updated["name"], "renamed")
        self.assertEqual([item[:2] for item in requests], [
            ("GET", "/api/v1/admin/accounts/17"),
            ("POST", "/api/v1/admin/accounts/bulk-update"),
            ("GET", "/api/v1/admin/accounts/17"),
        ])
        payload = requests[1][2]
        assert payload is not None
        self.assertEqual(payload["proxy_id"], 0)
        self.assertEqual(payload["group_ids"], [3])
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["extra"]["openai_oauth_responses_websockets_v2_mode"], "http_bridge")
        self.assertEqual(payload["extra"]["codex_image_generation_bridge"], True)
        self.assertEqual(payload["extra"]["auto_pause_5h_threshold"], 0.8)
        self.assertEqual(
            payload["credentials"],
            {"model_mapping": {"gpt-5.4": "gpt-5.4", "gpt-5.6": "gpt-5.6"}},
        )
        self.assertNotIn("access_token", json.dumps(payload))

    async def test_openai_advanced_configuration_builds_incremental_extra_patch(self) -> None:
        account = remote_account(type="oauth")
        configuration = AccountEditConfiguration(
            concurrency=5,
            priority=1,
            rate_multiplier=1,
            status="active",
            schedulable=True,
            proxy_id=None,
            group_ids=[],
            model_whitelist=[],
            openai_ws_mode="ctx_pool",
            codex_image_tool_mode="block",
            openai_passthrough=False,
            openai_long_context_billing=True,
            openai_compact_mode="auto",
            codex_cli_only=True,
            codex_cli_only_allow_app_server=True,
            auto_pause_5h_disabled=False,
            auto_pause_7d_disabled=True,
            auto_pause_5h_threshold_percent=80,
            auto_pause_7d_threshold_percent=0,
        )

        patch = account_extra_patch_from_configuration(configuration, account)

        self.assertEqual(patch["openai_oauth_responses_websockets_v2_mode"], "ctx_pool")
        self.assertIs(patch["openai_oauth_responses_websockets_v2_enabled"], True)
        self.assertEqual(patch["codex_image_generation_explicit_tool_policy"], "strip")
        self.assertIsNone(patch["codex_image_generation_bridge"])
        self.assertIsNone(patch["openai_compact_mode"])
        self.assertEqual(patch["auto_pause_5h_threshold"], 0.8)
        self.assertIsNone(patch["auto_pause_7d_threshold"])
        self.assertIs(patch["codex_cli_only_allow_app_server"], True)


if __name__ == "__main__":
    unittest.main()
