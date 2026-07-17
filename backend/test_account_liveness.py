from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.api.accounts import (
    get_account_liveness_models as account_liveness_models_endpoint,
    test_selected_account_liveness as account_liveness_endpoint,
)
from app.schemas import AccountLivenessModelsRequest, AccountLivenessTestRequest
from app.services.account_liveness import AccountLivenessLimiter
from app.services.runtime_config import EffectiveSub2ApiConfig
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError


def runtime_config() -> EffectiveSub2ApiConfig:
    return EffectiveSub2ApiConfig(
        base_url="http://sub2api.test/api/v1",
        auth_token="admin-token",
        auth_header="Authorization",
        auth_scheme="Bearer",
        accounts_path="/admin/accounts",
        access_token_path="credentials.access_token",
        auto_clear_error=True,
        auto_recover_state=True,
    )


class _BlockingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.Event().wait()
        yield b""


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class Sub2ApiLivenessClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_models_are_unwrapped_and_deduplicated(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": [
                    {"id": "gpt-5.4", "display_name": "GPT 5.4"},
                    {"id": "gpt-5.4", "display_name": "Duplicate"},
                    {"id": "gpt-5.3-codex"},
                    {"display_name": "Missing id"},
                ]
            }
        )
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=runtime_config()))

        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            models = await client.get_account_models("7")

        self.assertEqual(
            models,
            [
                {"id": "gpt-5.4", "display_name": "GPT 5.4"},
                {"id": "gpt-5.3-codex", "display_name": "gpt-5.3-codex"},
            ],
        )

    async def test_account_connection_reads_successful_sse_completion(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/admin/accounts/7/test")
            self.assertEqual(json.loads(request.content)["model_id"], "gpt-5.4")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    'data: {"type":"test_start","model":"gpt-5.4"}\n\n'
                    'data: {"type":"content","text":"ok"}\n\n'
                    'data: {"type":"test_complete","success":true}\n\n'
                ),
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=runtime_config()))
        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            success, error = await client.test_account_connection("7", "gpt-5.4")

        self.assertTrue(success)
        self.assertIsNone(error)

    async def test_account_connection_redacts_sse_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    'data: {"type":"error","error":"HTTP 401 Authorization: Bearer secret-value access_token=token-value"}\n\n'
                ),
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=runtime_config()))
        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            success, error = await client.test_account_connection("7", "gpt-5.4")

        self.assertFalse(success)
        self.assertIn("401", error or "")
        self.assertNotIn("secret-value", error or "")
        self.assertNotIn("token-value", error or "")

    async def test_account_connection_rejects_oversized_raw_sse_line(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_ChunkStream([b"data: " + (b"x" * 5_000)]),
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=runtime_config()))
        with (
            patch("app.services.sub2api.get_runtime_config_service", return_value=runtime),
            patch("app.services.sub2api.MAX_SUB2API_TEST_LINE_BYTES", 4_096, create=True),
        ):
            with self.assertRaisesRegex(Sub2ApiRequestError, "line was too large"):
                await client.test_account_connection("7", "gpt-5.4")

    async def test_account_connection_stops_raw_chunks_before_line_decoder_buffers_all(self) -> None:
        stream = _ChunkStream([b"x" * 16 for _ in range(10)])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=runtime_config()))
        with (
            patch("app.services.sub2api.get_runtime_config_service", return_value=runtime),
            patch("app.services.sub2api.MAX_SUB2API_TEST_STREAM_BYTES", 64),
            patch("app.services.sub2api.MAX_SUB2API_TEST_LINE_BYTES", 1_024),
        ):
            with self.assertRaisesRegex(Sub2ApiRequestError, "response was too large"):
                await client.test_account_connection("7", "gpt-5.4")

        self.assertEqual(stream.yielded, 5)

    async def test_account_connection_enforces_monotonic_total_deadline(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_BlockingStream(),
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=runtime_config()))
        with (
            patch("app.services.sub2api.get_runtime_config_service", return_value=runtime),
            patch("app.services.sub2api.SUB2API_TEST_TOTAL_TIMEOUT_SECONDS", 0.01, create=True),
        ):
            with self.assertRaisesRegex(Sub2ApiRequestError, "timed out"):
                await asyncio.wait_for(
                    client.test_account_connection("7", "gpt-5.4"),
                    timeout=0.2,
                )

    async def test_account_connection_propagates_cancellation(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_BlockingStream(),
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=runtime_config()))
        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            task = asyncio.create_task(client.test_account_connection("7", "gpt-5.4"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class AccountLivenessLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_is_shared_across_concurrent_batches(self) -> None:
        runtime = SimpleNamespace(
            get_account_liveness_max_concurrency=AsyncMock(return_value=2)
        )
        limiter = AccountLivenessLimiter(runtime)
        active = 0
        peak = 0

        async def worker() -> None:
            nonlocal active, peak
            async with limiter.slot():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(8)))

        self.assertEqual(peak, 2)
        self.assertEqual(limiter.active, 0)


class AccountLivenessBatchTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _oauth_account(account_id: int, name: str) -> dict:
        return {
            "id": account_id,
            "name": name,
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": f"{name}@example.com"},
        }

    async def test_model_lookup_falls_back_in_selection_order(self) -> None:
        first = self._oauth_account(1, "first")
        second = self._oauth_account(2, "second")
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(return_value=[second, first])  # type: ignore[method-assign]
        sub2api.get_account_models = AsyncMock(  # type: ignore[method-assign]
            side_effect=[Sub2ApiRequestError("first unavailable"), [{"id": "gpt-5.4", "display_name": "GPT 5.4"}]]
        )

        with patch("app.api.accounts.Sub2ApiClient", return_value=sub2api):
            result = await account_liveness_models_endpoint(
                AccountLivenessModelsRequest(account_ids=["1", "2"]),
                _={},
            )

        self.assertEqual(result.source_account_id, "2")
        self.assertEqual([model.id for model in result.models], ["gpt-5.4"])
        self.assertEqual(
            [call.args[0]["id"] for call in sub2api.get_account_models.await_args_list],
            [1, 2],
        )

    async def test_batch_returns_per_account_results_without_aborting(self) -> None:
        oauth_ok = {
            "id": 1,
            "name": "OAuth one",
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "one@example.com"},
        }
        oauth_failed = {
            "id": 2,
            "name": "OAuth two",
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "two@example.com"},
        }
        api_key = {
            "id": 4,
            "name": "API key",
            "platform": "openai",
            "type": "apikey",
            "credentials": {"email": "key@example.com"},
        }
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(return_value=[oauth_ok, oauth_failed, api_key])  # type: ignore[method-assign]
        sub2api.test_account_connection = AsyncMock(  # type: ignore[method-assign]
            side_effect=[(True, None), (False, "API returned 429")]
        )
        record = AsyncMock()

        with (
            patch("app.api.accounts.Sub2ApiClient", return_value=sub2api),
            patch("app.api.accounts.record_event", new=record),
        ):
            result = await account_liveness_endpoint(
                AccountLivenessTestRequest(
                    account_ids=["1", "2", "3", "4"],
                    model_id="gpt-5.4",
                ),
                _={},
                db=AsyncMock(),
            )

        self.assertEqual(result.total, 4)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, 3)
        self.assertTrue(result.results[0].success)
        self.assertEqual(result.results[1].error, "API returned 429")
        self.assertIn("找不到", result.results[2].error or "")
        self.assertIn("不是 OAuth", result.results[3].error or "")
        self.assertEqual(sub2api.test_account_connection.await_count, 2)
        record.assert_awaited_once()

    async def test_completed_liveness_result_survives_audit_failure(self) -> None:
        account = self._oauth_account(1, "first")
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(return_value=[account])  # type: ignore[method-assign]
        sub2api.test_account_connection = AsyncMock(return_value=(True, None))  # type: ignore[method-assign]
        db = AsyncMock()

        with (
            patch("app.api.accounts.Sub2ApiClient", return_value=sub2api),
            patch(
                "app.api.accounts.record_event",
                new=AsyncMock(side_effect=RuntimeError("audit unavailable")),
            ),
        ):
            result = await account_liveness_endpoint(
                AccountLivenessTestRequest(account_ids=["1"], model_id="gpt-5.4"),
                _={},
                db=db,
            )

        self.assertEqual(result.succeeded, 1)
        db.rollback.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
