from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.models import utcnow
from app.schemas import AccountEditConfiguration
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError


OPENAI_EDITABLE_ACCOUNT_TYPES = {"oauth", "setup-token", "apikey"}
OPENAI_CODEX_ACCOUNT_TYPES = {"oauth", "setup-token"}
OPENAI_WS_MODES = {"off", "ctx_pool", "passthrough", "http_bridge"}
OPENAI_COMPACT_MODES = {"auto", "force_on", "force_off"}


@dataclass(frozen=True)
class AccountEditorResources:
    account: dict[str, Any]
    groups: list[dict[str, Any]]
    proxies: list[dict[str, Any]]
    model_candidates: list[dict[str, str]]
    model_candidates_complete: bool
    checked_at: datetime


def account_identity_fingerprint(sub2api: Sub2ApiClient, account: dict[str, Any]) -> str:
    identity = [
        sub2api.account_id(account) or "",
        sub2api.account_name(account) or "",
        sub2api.account_platform(account) or "",
        sub2api.account_type(account) or "",
        sub2api.account_email(account) or "",
    ]
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_account_identity(
    sub2api: Sub2ApiClient,
    account: dict[str, Any],
    expected_fingerprint: str,
) -> None:
    if account_identity_fingerprint(sub2api, account) != expected_fingerprint:
        raise Sub2ApiRequestError(
            "sub2api account identity changed; reopen the editor before saving.",
            status_code=409,
        )


def account_configuration_from_remote(
    sub2api: Sub2ApiClient,
    account: dict[str, Any],
) -> AccountEditConfiguration:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    platform = (sub2api.account_platform(account) or "").strip().lower()
    account_type = (sub2api.account_type(account) or "").strip().lower()
    supports_openai_settings = platform == "openai" and account_type in OPENAI_EDITABLE_ACCOUNT_TYPES
    mapping = credentials.get("model_mapping") if isinstance(credentials, dict) else None
    model_whitelist = sorted(
        str(key)
        for key, value in (mapping.items() if isinstance(mapping, dict) else ())
        if isinstance(value, str) and str(key) == value
    )
    group_ids = sorted(
        value
        for raw in account.get("group_ids", [])
        if (value := _positive_int(raw)) is not None
    )

    openai_ws_mode = None
    codex_image_tool_mode = None
    openai_passthrough = None
    openai_long_context_billing = None
    openai_compact_mode = None
    codex_cli_only = None
    codex_cli_only_allow_app_server = None
    auto_pause_5h_disabled = None
    auto_pause_7d_disabled = None
    auto_pause_5h_threshold_percent = None
    auto_pause_7d_threshold_percent = None
    if supports_openai_settings:
        ws_mode_key = (
            "openai_apikey_responses_websockets_v2_mode"
            if account_type == "apikey"
            else "openai_oauth_responses_websockets_v2_mode"
        )
        ws_enabled_key = (
            "openai_apikey_responses_websockets_v2_enabled"
            if account_type == "apikey"
            else "openai_oauth_responses_websockets_v2_enabled"
        )
        openai_ws_mode = _openai_ws_mode(extra, ws_mode_key, ws_enabled_key)
        codex_image_tool_mode = _codex_image_tool_mode(extra)
        openai_passthrough = extra.get("openai_passthrough") is True or extra.get("openai_oauth_passthrough") is True
        openai_long_context_billing = extra.get("openai_long_context_billing_enabled") is True
        compact_mode = str(extra.get("openai_compact_mode") or "auto").strip().lower()
        openai_compact_mode = compact_mode if compact_mode in OPENAI_COMPACT_MODES else "auto"
        auto_pause_5h_disabled = extra.get("auto_pause_5h_disabled") is True
        auto_pause_7d_disabled = extra.get("auto_pause_7d_disabled") is True
        auto_pause_5h_threshold_percent = _threshold_percent(extra.get("auto_pause_5h_threshold"))
        auto_pause_7d_threshold_percent = _threshold_percent(extra.get("auto_pause_7d_threshold"))
        if account_type in OPENAI_CODEX_ACCOUNT_TYPES:
            codex_cli_only = extra.get("codex_cli_only") is True
            codex_cli_only_allow_app_server = extra.get("codex_cli_only_allow_app_server") is True

    raw_status = str(account.get("status") or "").strip().lower()
    return AccountEditConfiguration(
        concurrency=max(1, _nonnegative_int(account.get("concurrency"), default=1)),
        priority=_nonnegative_int(account.get("priority"), default=0),
        rate_multiplier=_bounded_float(account.get("rate_multiplier"), default=1.0),
        status=raw_status if raw_status in {"active", "inactive", "error"} else None,
        schedulable=account.get("schedulable") is not False,
        proxy_id=_positive_int(account.get("proxy_id")),
        group_ids=group_ids,
        model_whitelist=model_whitelist,
        openai_ws_mode=openai_ws_mode,
        codex_image_tool_mode=codex_image_tool_mode,
        openai_passthrough=openai_passthrough,
        openai_long_context_billing=openai_long_context_billing,
        openai_compact_mode=openai_compact_mode,
        codex_cli_only=codex_cli_only,
        codex_cli_only_allow_app_server=codex_cli_only_allow_app_server,
        auto_pause_5h_disabled=auto_pause_5h_disabled,
        auto_pause_7d_disabled=auto_pause_7d_disabled,
        auto_pause_5h_threshold_percent=auto_pause_5h_threshold_percent,
        auto_pause_7d_threshold_percent=auto_pause_7d_threshold_percent,
    )


def account_extra_patch_from_configuration(
    configuration: AccountEditConfiguration,
    account: dict[str, Any],
) -> dict[str, Any]:
    platform = str(account.get("platform") or "").strip().lower()
    account_type = str(account.get("type") or "").strip().lower()
    if platform != "openai" or account_type not in OPENAI_EDITABLE_ACCOUNT_TYPES:
        return {}

    patch: dict[str, Any] = {}
    if configuration.openai_ws_mode is not None:
        mode_key = (
            "openai_apikey_responses_websockets_v2_mode"
            if account_type == "apikey"
            else "openai_oauth_responses_websockets_v2_mode"
        )
        enabled_key = (
            "openai_apikey_responses_websockets_v2_enabled"
            if account_type == "apikey"
            else "openai_oauth_responses_websockets_v2_enabled"
        )
        patch[mode_key] = configuration.openai_ws_mode
        patch[enabled_key] = configuration.openai_ws_mode != "off"
        patch["responses_websockets_v2_enabled"] = None
        patch["openai_ws_enabled"] = None

    if configuration.codex_image_tool_mode is not None:
        patch["codex_image_generation_bridge_enabled"] = None
        if configuration.codex_image_tool_mode == "enabled":
            patch["codex_image_generation_bridge"] = True
            patch["codex_image_generation_explicit_tool_policy"] = None
        elif configuration.codex_image_tool_mode == "disabled":
            patch["codex_image_generation_bridge"] = False
            patch["codex_image_generation_explicit_tool_policy"] = None
        elif configuration.codex_image_tool_mode == "block":
            patch["codex_image_generation_bridge"] = None
            patch["codex_image_generation_explicit_tool_policy"] = "strip"
        else:
            patch["codex_image_generation_bridge"] = None
            patch["codex_image_generation_explicit_tool_policy"] = None

    if configuration.openai_passthrough is not None:
        patch["openai_passthrough"] = configuration.openai_passthrough
        patch["openai_oauth_passthrough"] = None
    if configuration.openai_long_context_billing is not None:
        patch["openai_long_context_billing_enabled"] = configuration.openai_long_context_billing
    if configuration.openai_compact_mode is not None:
        patch["openai_compact_mode"] = (
            None if configuration.openai_compact_mode == "auto" else configuration.openai_compact_mode
        )
    if configuration.auto_pause_5h_disabled is not None:
        patch["auto_pause_5h_disabled"] = configuration.auto_pause_5h_disabled
    if configuration.auto_pause_7d_disabled is not None:
        patch["auto_pause_7d_disabled"] = configuration.auto_pause_7d_disabled
    if configuration.auto_pause_5h_threshold_percent is not None:
        patch["auto_pause_5h_threshold"] = (
            configuration.auto_pause_5h_threshold_percent / 100
            if configuration.auto_pause_5h_threshold_percent > 0
            else None
        )
    if configuration.auto_pause_7d_threshold_percent is not None:
        patch["auto_pause_7d_threshold"] = (
            configuration.auto_pause_7d_threshold_percent / 100
            if configuration.auto_pause_7d_threshold_percent > 0
            else None
        )
    if account_type in OPENAI_CODEX_ACCOUNT_TYPES:
        if configuration.codex_cli_only is not None:
            patch["codex_cli_only"] = configuration.codex_cli_only
            patch["codex_cli_only_allowed_clients"] = None
        if configuration.codex_cli_only_allow_app_server is not None:
            patch["codex_cli_only_allow_app_server"] = configuration.codex_cli_only_allow_app_server
    return patch


async def load_account_editor_resources(
    sub2api: Sub2ApiClient,
    account_id: str | int,
) -> AccountEditorResources:
    account = await sub2api.get_account_by_id(account_id)
    if account is None:
        raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
    platform = sub2api.account_platform(account)
    if not platform:
        raise Sub2ApiRequestError("sub2api account does not expose a platform.")

    groups_result, proxies_result, candidates_result = await asyncio.gather(
        sub2api.list_groups_for_platform(platform),
        sub2api.list_proxies(),
        sub2api.list_account_model_candidates(platform),
        return_exceptions=True,
    )
    if isinstance(groups_result, BaseException):
        raise groups_result
    if isinstance(proxies_result, BaseException):
        raise proxies_result
    groups = groups_result
    proxies = proxies_result
    if isinstance(candidates_result, Sub2ApiRequestError) and candidates_result.status_code in {404, 405}:
        model_candidates = await sub2api.get_account_models(account)
        candidates_complete = False
    elif isinstance(candidates_result, BaseException):
        raise candidates_result
    else:
        model_candidates = candidates_result
        candidates_complete = True

    return AccountEditorResources(
        account=account,
        groups=groups,
        proxies=proxies,
        model_candidates=model_candidates,
        model_candidates_complete=candidates_complete,
        checked_at=utcnow(),
    )


def validate_account_edit_configuration(
    configuration: AccountEditConfiguration,
    resources: AccountEditorResources,
    *,
    preset_name: str | None = None,
) -> None:
    valid_group_ids = {
        value
        for item in resources.groups
        if (value := _positive_int(item.get("id"))) is not None
    }
    valid_proxy_ids = {
        value
        for item in resources.proxies
        if (value := _positive_int(item.get("id"))) is not None
    }
    valid_model_ids = {
        str(item.get("id") or "").strip()
        for item in resources.model_candidates
        if str(item.get("id") or "").strip()
    }

    problems: list[str] = []
    platform = str(resources.account.get("platform") or "").strip().lower()
    account_type = str(resources.account.get("type") or "").strip().lower()
    advanced_values = (
        configuration.openai_ws_mode,
        configuration.codex_image_tool_mode,
        configuration.openai_passthrough,
        configuration.openai_long_context_billing,
        configuration.openai_compact_mode,
        configuration.auto_pause_5h_disabled,
        configuration.auto_pause_7d_disabled,
        configuration.auto_pause_5h_threshold_percent,
        configuration.auto_pause_7d_threshold_percent,
    )
    if any(value is not None for value in advanced_values) and (
        platform != "openai" or account_type not in OPENAI_EDITABLE_ACCOUNT_TYPES
    ):
        problems.append("OpenAI / Codex 高级设置与当前账号类型不兼容")
    if (
        configuration.codex_cli_only is not None
        or configuration.codex_cli_only_allow_app_server is not None
    ) and (platform != "openai" or account_type not in OPENAI_CODEX_ACCOUNT_TYPES):
        problems.append("Codex CLI 限制仅适用于 OpenAI OAuth 或 Setup Token 账号")
    if configuration.codex_cli_only_allow_app_server is True and configuration.codex_cli_only is False:
        problems.append("启用 App Server 放行前必须先启用 Codex CLI 限制")
    missing_groups = sorted(set(configuration.group_ids) - valid_group_ids)
    if missing_groups:
        problems.append("分组 " + ", ".join(f"#{value}" for value in missing_groups) + " 已删除或停用")
    if configuration.proxy_id is not None and configuration.proxy_id not in valid_proxy_ids:
        problems.append(f"代理 #{configuration.proxy_id} 已删除或停用")
    if configuration.model_whitelist and not resources.model_candidates_complete:
        problems.append("当前 sub2api 版本未提供完整模型候选列表")
    elif configuration.model_whitelist:
        missing_models = sorted(set(configuration.model_whitelist) - valid_model_ids)
        if missing_models:
            problems.append("模型 " + ", ".join(missing_models) + " 已不可用")
    if problems:
        prefix = f"模板“{preset_name}”已失效" if preset_name else "账号配置已失效"
        raise ValueError(prefix + "：" + "；".join(problems) + "。")


def resource_id(item: dict[str, Any]) -> int | None:
    return _positive_int(item.get("id"))


def _openai_ws_mode(extra: dict[str, Any], mode_key: str, enabled_key: str) -> str:
    raw_mode = str(extra.get(mode_key) or "").strip().lower()
    if raw_mode in {"shared", "dedicated"}:
        return "ctx_pool"
    if raw_mode in OPENAI_WS_MODES:
        return raw_mode
    for key in (enabled_key, "responses_websockets_v2_enabled", "openai_ws_enabled"):
        enabled = extra.get(key)
        if isinstance(enabled, bool):
            return "ctx_pool" if enabled else "off"
    return "off"


def _codex_image_tool_mode(extra: dict[str, Any]) -> str:
    if extra.get("codex_image_generation_explicit_tool_policy") == "strip":
        return "block"
    bridge = extra.get("codex_image_generation_bridge")
    if not isinstance(bridge, bool):
        bridge = extra.get("codex_image_generation_bridge_enabled")
    if bridge is True:
        return "enabled"
    if bridge is False:
        return "disabled"
    return "inherit"


def _threshold_percent(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed * 100 if 0 < parsed <= 1 else 0.0


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 1 <= parsed <= 9_007_199_254_740_991 else None


def _nonnegative_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= 9_007_199_254_740_991 else default


def _bounded_float(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if 0 <= parsed <= 1000 else default
