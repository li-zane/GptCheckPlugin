import asyncio
import base64
import calendar
import copy
import json
import math
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.upstream_urls import canonicalize_upstream_url
from app.services.runtime_config import EffectiveSub2ApiConfig, get_runtime_config_service


EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
DEACTIVE_MARKERS = (
    "account_deactive",
    "account_deactivated",
    "account deactive",
    "account deactivated",
    "account has been deactivated",
    "account is deactivated",
    "openai account has been deactivated",
    "账号已停用",
    "账户已停用",
    "账号被停用",
    "账户被停用",
)
ACCOUNT_EMAIL_PATHS = (
    ("credentials", "email"),
    ("credentials", "account_email"),
    ("credentials", "account"),
    ("credentials", "username"),
    ("credentials", "user", "email"),
    ("credentials", "user"),
    ("credentials", "login"),
    ("extra", "email"),
    ("extra", "account_email"),
    ("extra", "account"),
    ("extra", "username"),
    ("extra", "user", "email"),
    ("extra", "user"),
    ("extra", "login"),
    ("profile", "email"),
    ("email",),
    ("account_email",),
    ("username",),
    ("user", "email"),
    ("user",),
    ("login",),
)
ACCOUNT_NAME_PATHS = (
    ("name",),
    ("account_name",),
    ("accountName",),
    ("profile", "name"),
    ("extra", "name"),
    ("credentials", "name"),
)
# A 100-account page can exceed 1 MiB because sub2api includes OAuth metadata.
MAX_SUB2API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SUB2API_ERROR_PREVIEW_BYTES = 500
MAX_SUB2API_TEST_STREAM_BYTES = 64 * 1024
MAX_SUB2API_TEST_LINE_BYTES = 16 * 1024
SUB2API_REQUEST_TOTAL_TIMEOUT_SECONDS = 90.0
SUB2API_USAGE_REFRESH_TIMEOUT_SECONDS = 10.0
SUB2API_TODAY_STATS_TIMEOUT_SECONDS = 10.0
SUB2API_TODAY_STATS_BATCH_SIZE = 100
SUB2API_DAILY_STATS_MAX_DAYS = 366
SUB2API_DAILY_STATS_CONCURRENCY = 8
SUB2API_TEST_TOTAL_TIMEOUT_SECONDS = 70.0
MAX_REMOTE_ACCOUNT_ERROR_CHARS = 500
MAX_SUB2API_ACCOUNT_PAGES = 100
MAX_SUB2API_ACCOUNTS = 10_000
MAX_SUB2API_PRIORITY = 9_007_199_254_740_991
MAX_EXPORTED_OAUTH_TOKEN_LENGTH = 65_536
MAX_EXPORTED_OAUTH_CLIENT_ID_LENGTH = 4_096
MAX_EXPORTED_OAUTH_METADATA_LENGTH = 512
SUB2API_MUTATION_READBACK_ATTEMPTS = 3
SUB2API_MUTATION_READBACK_DELAY_SECONDS = 0.1
SUB2API_MUTATION_READBACK_TIMEOUT_SECONDS = 10.0
SENSITIVE_ERROR_FIELD_PATTERN = (
    r"(?:access[-_]?token|refresh[-_]?token|id[-_]?token|token|rt|"
    r"api[-_]?key|apikey|x[-_]?api[-_]?key|password|client[-_]?secret|secret|"
    r"authorization|proxy[-_]?authorization|cookie|set[-_]?cookie)"
)
SENSITIVE_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie)"
    r"\s*:\s*[^\r\n]*"
)
SENSITIVE_QUERY_RE = re.compile(
    rf"(?i)([?&]{SENSITIVE_ERROR_FIELD_PATTERN}=)[^&#\s]*"
)
SENSITIVE_ERROR_RE = re.compile(
    rf"(?i)([\"']?{SENSITIVE_ERROR_FIELD_PATTERN}[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)"
)
SENSITIVE_ERROR_BRIDGE_RE = re.compile(
    r"(?i)(\b(?:x[-_\s]?api[-_\s]?key|api[-_\s]?key)\b"
    r"\s+(?:is|was|provided|supplied)\s*:\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)"
)
AUTH_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\r\n]*")


class Sub2ApiRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def redact_sub2api_error_text(
    value: Any,
    *,
    known_credentials: tuple[str, ...] | list[str] | set[str] = (),
    limit: int | None = MAX_REMOTE_ACCOUNT_ERROR_CHARS,
    strip_whitespace: bool = True,
) -> str:
    text = str(value or "")
    text = SENSITIVE_HEADER_RE.sub(
        lambda match: f"{match.group(1)}: ***redacted***",
        text,
    )
    text = SENSITIVE_QUERY_RE.sub(r"\1***redacted***", text)
    text = SENSITIVE_ERROR_RE.sub(r"\1***redacted***", text)
    text = SENSITIVE_ERROR_BRIDGE_RE.sub(r"\1***redacted***", text)
    text = AUTH_SCHEME_RE.sub(r"\1 ***redacted***", text)
    for credential in sorted(
        {str(item) for item in known_credentials if str(item)},
        key=len,
        reverse=True,
    ):
        text = text.replace(credential, "***redacted***")
    if strip_whitespace:
        text = text.strip()
    if limit is None:
        return text
    return text[: max(0, limit)]


async def _read_bounded_response(
    response: httpx.Response,
    *,
    limit: int,
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        remaining = limit - size
        if remaining <= 0:
            return b"".join(chunks), True
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


async def _iter_bounded_response_lines(response: httpx.Response):
    pending = bytearray()
    consumed = 0
    async for chunk in response.aiter_bytes():
        consumed += len(chunk)
        if consumed > MAX_SUB2API_TEST_STREAM_BYTES:
            raise Sub2ApiRequestError("sub2api account test response was too large.")
        pending.extend(chunk)
        while True:
            newline_at = pending.find(b"\n")
            if newline_at < 0:
                if len(pending) > MAX_SUB2API_TEST_LINE_BYTES:
                    raise Sub2ApiRequestError("sub2api account test response line was too large.")
                break
            raw_line = bytes(pending[:newline_at])
            del pending[: newline_at + 1]
            if len(raw_line) > MAX_SUB2API_TEST_LINE_BYTES:
                raise Sub2ApiRequestError("sub2api account test response line was too large.")
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            yield raw_line.decode("utf-8", errors="replace")

    if pending:
        if len(pending) > MAX_SUB2API_TEST_LINE_BYTES:
            raise Sub2ApiRequestError("sub2api account test response line was too large.")
        yield bytes(pending).decode("utf-8", errors="replace")


def _deep_get(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _deep_set(data: dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def find_email(value: Any) -> str | None:
    if isinstance(value, dict):
        priority_keys = ("email", "account", "username", "user", "login")
        for key in priority_keys:
            candidate = value.get(key)
            if isinstance(candidate, str):
                match = EMAIL_RE.search(candidate)
                if match:
                    return match.group(0).lower()
        for child in value.values():
            found = find_email(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_email(child)
            if found:
                return found
    elif isinstance(value, str):
        match = EMAIL_RE.search(value)
        if match:
            return match.group(0).lower()
    return None


def extract_email(value: Any) -> str | None:
    if isinstance(value, str):
        match = EMAIL_RE.search(value.strip())
        if match:
            return match.group(0).lower()
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: Any, *, maximum: int = MAX_SUB2API_PRIORITY) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed <= maximum else None


def _find_nested_field(value: Any, field: str) -> tuple[bool, Any]:
    if not isinstance(value, dict):
        return False, None
    if field in value:
        return True, value[field]
    for key in ("data", "settings", "payment", "payment_settings", "config"):
        child = value.get(key)
        if isinstance(child, dict):
            found, result = _find_nested_field(child, field)
            if found:
                return True, result
    return False, None


def _bounded_number(value: Any, *, minimum: float = 0, maximum: float = 1000) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        return None
    return parsed


def _parse_account_today_costs(payload: Any, expected_ids: set[int]) -> dict[int, float]:
    if not expected_ids or not isinstance(payload, (dict, list)):
        return {}
    if isinstance(payload, dict):
        if payload.get("success") is False:
            return {}
        code = payload.get("code")
        if code is not None and code not in {0, 200, "0", "200"}:
            return {}
        data: Any = payload.get("data", payload)
    else:
        data = payload
    if isinstance(data, dict):
        for key in ("stats", "results", "items"):
            if isinstance(data.get(key), (dict, list)):
                data = data[key]
                break

    parsed: dict[int, float] = {}
    records: list[tuple[Any, Any]] = []
    if isinstance(data, list):
        records = [(None, item) for item in data]
    elif isinstance(data, dict):
        records = list(data.items())
    for keyed_id, raw in records:
        explicit_id = (
            raw.get("account_id", raw.get("accountId", raw.get("id")))
            if isinstance(raw, dict)
            else None
        )
        account_id = _positive_int(explicit_id if explicit_id is not None else keyed_id)
        if account_id not in expected_ids:
            continue
        raw_cost = (
            next(
                (
                    raw.get(field)
                    for field in (
                        "today_actual_cost",
                        "todayActualCost",
                        "actual_cost",
                        "actualCost",
                    )
                    if raw.get(field) is not None
                ),
                None,
            )
            if isinstance(raw, dict)
            else raw
        )
        cost = _bounded_number(raw_cost, minimum=0, maximum=1_000_000_000_000_000)
        if cost is not None:
            parsed[account_id] = cost
    return parsed


def _mapping_patch_matches(source: dict[str, Any], patch: dict[str, Any]) -> bool:
    for key, expected in patch.items():
        actual = source.get(key)
        if isinstance(expected, bool):
            if actual is not expected:
                return False
        elif isinstance(expected, (int, float)):
            confirmed = _bounded_number(actual, minimum=-1_000_000, maximum=1_000_000)
            if confirmed is None or not math.isclose(confirmed, float(expected), rel_tol=1e-9, abs_tol=1e-9):
                return False
        elif expected is None:
            if actual is not None:
                return False
        elif actual != expected:
            return False
    return True


def _parse_account_daily_costs(payload: Any) -> dict[date, float]:
    """Extract per-calendar-day actual costs from one account's stats payload."""

    if not isinstance(payload, dict) or payload.get("success") is False:
        return {}
    code = payload.get("code")
    if code is not None and code not in {0, 200, "0", "200"}:
        return {}
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return {}
    history = data.get("history")
    if not isinstance(history, list):
        return {}

    parsed: dict[date, float] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        raw_day = item.get("date")
        if not isinstance(raw_day, str):
            continue
        try:
            usage_date = date.fromisoformat(raw_day.strip()[:10])
        except ValueError:
            continue
        raw_cost = next(
            (
                item.get(field)
                for field in (
                    "actual_cost",
                    "actualCost",
                    "today_actual_cost",
                    "todayActualCost",
                )
                if item.get(field) is not None
            ),
            None,
        )
        cost = _bounded_number(raw_cost, minimum=0, maximum=1_000_000_000_000_000)
        if cost is not None:
            parsed[usage_date] = cost
    return parsed


def looks_deactive_text(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    text = value.lower()
    return any(marker in text for marker in DEACTIVE_MARKERS)


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            normalized = re.sub(r"[^a-z0-9]", "", lowered)
            if (
                normalized == "rt"
                or "apikey" in normalized
                or any(
                    marker in normalized
                    for marker in ("token", "secret", "password", "cookie", "authorization")
                )
            ):
                sanitized[key] = "***redacted***" if child else child
            else:
                sanitized[key] = sanitize_payload(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(child) for child in value]
    if isinstance(value, str):
        return redact_sub2api_error_text(
            value,
            limit=None,
            strip_whitespace=False,
        )
    return value


SENSITIVE_CREDENTIAL_KEYS = {
    "access_token",
    "refresh_token",
    "rt",
    "id_token",
    "api_key",
    "session_key",
    "cookie",
    "aws_secret_access_key",
    "aws_session_token",
    "service_account_json",
    "service_account",
    "private_key",
}
REDACTED_VALUES = {"***redacted***", "[redacted]", "[REDACTED]", "***", "********"}
SUBSCRIPTION_CREDENTIAL_KEYS = (
    "plan_type",
    "subscription_starts_at",
    "subscription_expires_at",
    "subscription_renews_at",
    "subscription_cancels_at",
    "subscription_billing_period",
    "subscription_plan",
    "has_active_subscription",
)
HEALTHY_ACCOUNT_STATUSES = {"active", "ok", "ready", "normal", "recovered", "healthy", "valid"}


def _path_get(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _first_value(data: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _path_get(data, path)
        if value is not None:
            return value
    return None


def _first_string(data: Any, *paths: tuple[str, ...]) -> str | None:
    value = _first_value(data, *paths)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _set_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, str):
        value = value.strip()
    if value not in (None, ""):
        target[key] = value


def _decode_jwt_payload(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    parts = token.strip().split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    padded = segment + ("=" * (-len(segment) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _unix_to_rfc3339(value: int | float) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_rfc3339(value: Any) -> str | None:
    epoch = _time_to_epoch(value)
    if epoch is not None:
        return _unix_to_rfc3339(epoch)
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shift_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _subscription_starts_at(end_at: str | None, billing_period: str | None) -> str | None:
    end = _parse_datetime(end_at)
    if end is None or not billing_period:
        return None
    period = billing_period.strip().lower()
    if period in {"monthly", "month"}:
        return _format_datetime(_shift_months(end, -1))
    if period in {"yearly", "annual", "annually", "year"}:
        return _format_datetime(_shift_months(end, -12))
    if period in {"weekly", "week"}:
        return _format_datetime(end - timedelta(days=7))
    return None


def _time_to_epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000
        return int(number)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return _time_to_epoch(float(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def _credential_values_equal(key: str, current: Any, candidate: Any) -> bool:
    if key in {"expires_at", "subscription_expires_at", "subscription_renews_at", "subscription_cancels_at"}:
        current_epoch = _time_to_epoch(current)
        candidate_epoch = _time_to_epoch(candidate)
        if current_epoch is not None and candidate_epoch is not None:
            return current_epoch == candidate_epoch
    return current == candidate


def _looks_redacted(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text in REDACTED_VALUES or (len(text) >= 6 and set(text) <= {"*"})


def _default_organization_id(openai_auth: dict[str, Any]) -> str | None:
    poid = openai_auth.get("poid")
    if isinstance(poid, str) and poid.strip():
        return poid.strip()
    organizations = openai_auth.get("organizations")
    if not isinstance(organizations, list):
        return None
    first_id: str | None = None
    for organization in organizations:
        if not isinstance(organization, dict):
            continue
        org_id = organization.get("id")
        if not isinstance(org_id, str) or not org_id.strip():
            continue
        if first_id is None:
            first_id = org_id.strip()
        if organization.get("is_default") is True:
            return org_id.strip()
    return first_id


class Sub2ApiClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self._known_credentials: set[str] = set()
        configured_token = str(getattr(self.settings, "sub2api_auth_token", "") or "").strip()
        if configured_token:
            self._known_credentials.add(configured_token)

    def _remember_config_credential(self, config: EffectiveSub2ApiConfig) -> None:
        token = str(config.auth_token or "").strip()
        if token:
            self._known_credentials.add(token)

    def _redact_error_text(
        self,
        value: Any,
        *,
        limit: int = MAX_REMOTE_ACCOUNT_ERROR_CHARS,
    ) -> str:
        return redact_sub2api_error_text(
            value,
            known_credentials=self._known_credentials,
            limit=limit,
        )

    def redact_error_text(
        self,
        value: Any,
        *,
        limit: int = MAX_REMOTE_ACCOUNT_ERROR_CHARS,
    ) -> str:
        return self._redact_error_text(value, limit=limit)

    def _headers(self, config: EffectiveSub2ApiConfig) -> dict[str, str]:
        self._remember_config_credential(config)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        token = config.auth_token.strip()
        if token:
            scheme = config.auth_scheme.strip()
            value = f"{scheme} {token}".strip() if scheme else token
            headers[config.auth_header] = value
        return headers

    def _url(self, config: EffectiveSub2ApiConfig, path: str) -> str:
        return f"{config.base_url}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        config: EffectiveSub2ApiConfig | None = None,
        total_timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> Any:
        request_timeout = (
            SUB2API_REQUEST_TOTAL_TIMEOUT_SECONDS
            if total_timeout_seconds is None
            else max(0.1, float(total_timeout_seconds))
        )
        deadline = (
            asyncio.get_running_loop().time()
            + request_timeout
        )
        try:
            async with asyncio.timeout_at(deadline):
                active_config = config or await get_runtime_config_service().get_sub2api_config()
                async with httpx.AsyncClient(
                    timeout=min(30.0, request_timeout),
                    headers=self._headers(active_config),
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    url = self._url(active_config, path)
                    async with client.stream(method, url, **kwargs) as response:
                        status_code = response.status_code
                        if status_code < 200 or status_code >= 300:
                            _body, truncated = await _read_bounded_response(
                                response,
                                limit=MAX_SUB2API_ERROR_PREVIEW_BYTES,
                            )
                            body_state = "exceeded the diagnostic limit" if truncated else "was omitted"
                            raise Sub2ApiRequestError(
                                self._redact_error_text(
                                    f"sub2api request failed: HTTP {status_code} for {method} {url}; "
                                    f"remote response body {body_state}."
                                ),
                                status_code=status_code,
                            )
                        body, truncated = await _read_bounded_response(
                            response,
                            limit=MAX_SUB2API_RESPONSE_BYTES,
                        )
                    if truncated:
                        raise Sub2ApiRequestError(
                            f"sub2api response exceeded {MAX_SUB2API_RESPONSE_BYTES} bytes for {method} {url}."
                        )
                    if not body:
                        return None
                    try:
                        return json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise Sub2ApiRequestError(
                            self._redact_error_text(
                                f"sub2api returned non-JSON response for {method} {url}; "
                                "remote response body was omitted."
                            )
                        ) from exc
        except Sub2ApiRequestError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise Sub2ApiRequestError("sub2api request timed out.") from exc

    def _unwrap(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                return payload["data"]
            if isinstance(payload.get("data"), dict):
                data = payload["data"]
                for key in ("items", "records", "accounts", "list"):
                    if isinstance(data.get(key), list):
                        return data[key]
                return data
            for key in ("items", "records", "accounts", "list"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return payload

    async def list_accounts(
        self,
        *,
        config: EffectiveSub2ApiConfig | None = None,
    ) -> list[dict[str, Any]]:
        config = config or await get_runtime_config_service().get_sub2api_config()
        payload = await self._request("GET", config.accounts_path, config=config, params={"page": 1, "page_size": 100})
        accounts = self._unwrap(payload)
        if not isinstance(accounts, list):
            return []

        result = [item for item in accounts if isinstance(item, dict)]
        if len(result) > MAX_SUB2API_ACCOUNTS:
            raise Sub2ApiRequestError("sub2api returned too many accounts.")
        data = payload.get("data") if isinstance(payload, dict) else None
        pages = _positive_int(data.get("pages") if isinstance(data, dict) else None)
        if pages is not None and pages > MAX_SUB2API_ACCOUNT_PAGES:
            raise Sub2ApiRequestError("sub2api account pagination exceeds the safety limit.")
        if pages is not None and pages > 1:
            for page in range(2, pages + 1):
                page_payload = await self._request(
                    "GET",
                    config.accounts_path,
                    config=config,
                    params={"page": page, "page_size": 100},
                )
                page_accounts = self._unwrap(page_payload)
                if isinstance(page_accounts, list):
                    result.extend(item for item in page_accounts if isinstance(item, dict))
                    if len(result) > MAX_SUB2API_ACCOUNTS:
                        raise Sub2ApiRequestError("sub2api returned too many accounts.")
        return result

    async def list_api_key_accounts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for account in await self.list_accounts():
            account_type = (self.account_type(account) or "").strip().lower()
            normalized = account_type.replace("-", "_").replace(" ", "_")
            if normalized in {"apikey", "api_key"}:
                result.append(account)
        return result

    async def get_account_by_id(
        self,
        account_id: str | int,
        *,
        config: EffectiveSub2ApiConfig | None = None,
    ) -> dict[str, Any] | None:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        config = config or await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request(
                "GET",
                f"{config.accounts_path}/{numeric_id}",
                config=config,
            )
        except Sub2ApiRequestError as exc:
            if exc.status_code == 404:
                return None
            raise
        account = self._unwrap(payload)
        if not isinstance(account, dict):
            raise Sub2ApiRequestError("sub2api returned an invalid account response.")
        if self.account_id(account) != str(numeric_id):
            raise Sub2ApiRequestError("sub2api returned a mismatched account response.")
        return account

    def _api_key_export_identity(
        self,
        account: dict[str, Any],
    ) -> tuple[str, str] | None:
        name = self.account_name(account)
        raw_type = _first_value(
            account,
            ("type",),
            ("account_type",),
            ("auth_type",),
        )
        if not name or not isinstance(raw_type, str):
            return None
        normalized_type = raw_type.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized_type not in {"apikey", "api_key"}:
            return None
        return name, "api_key"

    def _api_key_account_identity(
        self,
        account: dict[str, Any],
    ) -> tuple[int, str, str] | None:
        account_id = _positive_int(self.account_id(account))
        export_identity = self._api_key_export_identity(account)
        if account_id is None or export_identity is None:
            return None
        return account_id, *export_identity

    async def export_api_key_secrets(self, account_ids: list[int]) -> dict[int, str]:
        """Read API keys from the protected local admin export without caching them."""

        normalized_ids: list[int] = []
        for raw_id in account_ids:
            account_id = _positive_int(raw_id)
            if account_id is None or account_id in normalized_ids:
                continue
            normalized_ids.append(account_id)
        if not normalized_ids or len(normalized_ids) > 200:
            return {}

        config = await get_runtime_config_service().get_sub2api_config()

        async def request_export(account_id: int) -> list[Any] | None:
            try:
                payload = await self._request(
                    "GET",
                    f"{config.accounts_path}/data",
                    config=config,
                    params={"ids": str(account_id), "include_proxies": "false"},
                )
            except Sub2ApiRequestError:
                return None
            unwrapped = self._unwrap(payload)
            return unwrapped if isinstance(unwrapped, list) else None

        def exported_key(item: Any) -> str | None:
            if not isinstance(item, dict):
                return None
            credentials = item.get("credentials")
            if not isinstance(credentials, dict):
                return None
            raw_key = _first_value(
                credentials,
                ("api_key",),
                ("apiKey",),
                ("apikey",),
            )
            if not isinstance(raw_key, str):
                return None
            api_key = raw_key.strip()
            if (
                not api_key
                or len(api_key) > 8192
                or _looks_redacted(api_key)
                or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
            ):
                return None
            return api_key

        result: dict[int, str] = {}
        for account_id in normalized_ids:
            try:
                before = await self.get_account_by_id(account_id, config=config)
            except Sub2ApiRequestError:
                continue
            before_identity = (
                self._api_key_account_identity(before)
                if isinstance(before, dict)
                else None
            )
            if before_identity is None or before_identity[0] != account_id:
                continue

            exported = await request_export(account_id)
            if exported is None or len(exported) != 1 or not isinstance(exported[0], dict):
                continue
            item = exported[0]
            raw_exported_id = _first_value(
                item,
                ("id",),
                ("account_id",),
                ("accountId",),
            )
            if raw_exported_id is not None and _positive_int(raw_exported_id) != account_id:
                continue
            if self._api_key_export_identity(item) != before_identity[1:]:
                continue
            api_key = exported_key(item)
            if api_key is None:
                continue

            try:
                after = await self.get_account_by_id(account_id, config=config)
            except Sub2ApiRequestError:
                continue
            after_identity = (
                self._api_key_account_identity(after)
                if isinstance(after, dict)
                else None
            )
            if after_identity != before_identity:
                continue
            result[account_id] = api_key
        return result

    async def export_oauth_credentials(
        self,
        account_ids: list[int],
    ) -> dict[int, dict[str, Any]]:
        """Export an ID-bound, allowlisted OAuth credential set from sub2api."""

        normalized_ids: list[int] = []
        for raw_id in account_ids:
            account_id = _positive_int(raw_id)
            if account_id is None or account_id in normalized_ids:
                continue
            normalized_ids.append(account_id)
        if not normalized_ids or len(normalized_ids) > 200:
            return {}

        config = await get_runtime_config_service().get_sub2api_config()
        export_params = {
            "ids": ",".join(str(account_id) for account_id in normalized_ids),
            "include_proxies": "false",
        }

        async def request_export() -> list[Any] | None:
            try:
                payload = await self._request(
                    "GET",
                    f"{config.accounts_path}/data",
                    config=config,
                    params=export_params,
                )
            except Sub2ApiRequestError:
                return None
            unwrapped = self._unwrap(payload)
            return unwrapped if isinstance(unwrapped, list) else None

        def exported_secret(item: dict[str, Any], *aliases: str, maximum: int) -> str | None:
            credentials = item.get("credentials")
            if not isinstance(credentials, dict):
                return None
            raw_value = _first_value(credentials, *((alias,) for alias in aliases))
            if not isinstance(raw_value, str):
                return None
            value = raw_value.strip()
            if (
                not value
                or len(value) > maximum
                or _looks_redacted(value)
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                return None
            return value

        def exported_credentials(item: Any) -> dict[str, Any] | None:
            if not isinstance(item, dict) or not self.is_oauth_account(item):
                return None
            access_token = exported_secret(
                item,
                "access_token",
                "accessToken",
                maximum=MAX_EXPORTED_OAUTH_TOKEN_LENGTH,
            )
            refresh_token = exported_secret(
                item,
                "refresh_token",
                "refreshToken",
                "rt",
                maximum=MAX_EXPORTED_OAUTH_TOKEN_LENGTH,
            )
            id_token = exported_secret(
                item,
                "id_token",
                "idToken",
                maximum=MAX_EXPORTED_OAUTH_TOKEN_LENGTH,
            )
            client_id = exported_secret(
                item,
                "client_id",
                "clientId",
                maximum=MAX_EXPORTED_OAUTH_CLIENT_ID_LENGTH,
            )
            result: dict[str, Any] = {}
            for key, value in (
                ("access_token", access_token),
                ("refresh_token", refresh_token),
                ("id_token", id_token),
                ("client_id", client_id),
            ):
                if value:
                    result[key] = value

            credentials = item.get("credentials")
            contexts = [source for source in (credentials, item) if isinstance(source, dict)]
            for source in contexts:
                derived = self.credentials_from_session(source, access_token or "")
                for key in ("expires_at", "plan_type", *SUBSCRIPTION_CREDENTIAL_KEYS):
                    value = derived.get(key)
                    if isinstance(value, bool):
                        result[key] = value
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        result[key] = value
                    elif isinstance(value, str):
                        normalized = value.strip()
                        if normalized and len(normalized) <= MAX_EXPORTED_OAUTH_METADATA_LENGTH:
                            result[key] = normalized
            return result or None

        requested = set(normalized_ids)
        exported = await request_export()
        if exported is None:
            return {}

        raw_response_ids = [
            _first_value(item, ("id",), ("account_id",), ("accountId",))
            if isinstance(item, dict)
            else None
            for item in exported
        ]
        if any(value is not None for value in raw_response_ids):
            result: dict[int, dict[str, Any]] = {}
            duplicate_ids: set[int] = set()
            for raw_id, item in zip(raw_response_ids, exported, strict=True):
                account_id = _positive_int(raw_id)
                if account_id is None or account_id not in requested:
                    continue
                if account_id in result:
                    duplicate_ids.add(account_id)
                    continue
                credentials = exported_credentials(item)
                if credentials:
                    result[account_id] = credentials
            for account_id in duplicate_ids:
                result.pop(account_id, None)
            return result

        # Backup-style exports omit database IDs. Discard the format probe and
        # accept a second export only when the requested ID-to-email inventory
        # remains stable around it.
        def inventory_by_id(accounts: list[dict[str, Any]]) -> dict[int, str] | None:
            by_id: dict[int, str] = {}
            emails: set[str] = set()
            for account in accounts:
                account_id = _positive_int(self.account_id(account))
                if account_id not in requested:
                    continue
                email = self.account_email(account)
                if (
                    account_id is None
                    or not email
                    or not self.is_gpt_account(account)
                    or not self.is_oauth_account(account)
                ):
                    return None
                normalized_email = email.strip().lower()
                if account_id in by_id or normalized_email in emails:
                    return None
                by_id[account_id] = normalized_email
                emails.add(normalized_email)
            return by_id if set(by_id) == requested else None

        try:
            before_accounts = await self.list_accounts(config=config)
        except Sub2ApiRequestError:
            return {}
        exported = await request_export()
        if exported is None:
            return {}
        if any(
            _first_value(item, ("id",), ("account_id",), ("accountId",)) is not None
            for item in exported
            if isinstance(item, dict)
        ):
            return {}
        try:
            after_accounts = await self.list_accounts(config=config)
        except Sub2ApiRequestError:
            return {}

        before_by_id = inventory_by_id(before_accounts)
        after_by_id = inventory_by_id(after_accounts)
        if before_by_id is None or after_by_id is None or before_by_id != after_by_id:
            return {}

        exported_by_email: dict[str, dict[str, Any]] = {}
        for item in exported:
            if not isinstance(item, dict):
                return {}
            email = self.account_email(item)
            credentials = exported_credentials(item)
            if not email or credentials is None:
                return {}
            normalized_email = email.strip().lower()
            if normalized_email in exported_by_email:
                return {}
            exported_by_email[normalized_email] = credentials

        expected_emails = set(before_by_id.values())
        if set(exported_by_email) != expected_emails:
            return {}
        return {
            account_id: exported_by_email[email]
            for account_id, email in before_by_id.items()
        }

    async def get_account_balance(self, account: dict[str, Any] | str | int) -> dict[str, Any]:
        raw_account_id = account if isinstance(account, (str, int)) else self.account_id(account)
        account_id = _positive_int(raw_account_id)
        if account_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request(
            "GET",
            f"{config.accounts_path}/{account_id}/balance",
            config=config,
        )
        balance = self._unwrap(payload)
        return balance if isinstance(balance, dict) else {}

    async def get_account_today_costs(
        self,
        account_ids: list[int],
    ) -> dict[int, float]:
        normalized_ids: list[int] = []
        seen: set[int] = set()
        for account_id in account_ids:
            parsed_id = _positive_int(account_id)
            if parsed_id is None:
                raise ValueError("Account ids must be positive integers.")
            if parsed_id not in seen:
                normalized_ids.append(parsed_id)
                seen.add(parsed_id)
        if len(normalized_ids) > MAX_SUB2API_ACCOUNTS:
            raise ValueError("Too many account ids were provided.")
        if not normalized_ids:
            return {}

        config = await get_runtime_config_service().get_sub2api_config()
        costs: dict[int, float] = {}
        try:
            async with asyncio.timeout(SUB2API_TODAY_STATS_TIMEOUT_SECONDS):
                for offset in range(0, len(normalized_ids), SUB2API_TODAY_STATS_BATCH_SIZE):
                    batch = normalized_ids[offset : offset + SUB2API_TODAY_STATS_BATCH_SIZE]
                    payload = await self._request(
                        "POST",
                        "/admin/accounts/today-stats/batch",
                        config=config,
                        total_timeout_seconds=SUB2API_TODAY_STATS_TIMEOUT_SECONDS,
                        json={"account_ids": batch},
                    )
                    costs.update(_parse_account_today_costs(payload, set(batch)))
        except TimeoutError as exc:
            raise Sub2ApiRequestError("sub2api today statistics request timed out.") from exc
        return costs

    async def get_account_daily_costs(
        self,
        account_ids: list[int],
        *,
        days: int = 2,
    ) -> dict[int, dict[date, float]]:
        """Return historic actual-cost readings keyed by account and local date."""

        normalized_ids: list[int] = []
        seen: set[int] = set()
        for account_id in account_ids:
            parsed_id = _positive_int(account_id)
            if parsed_id is None:
                raise ValueError("Account ids must be positive integers.")
            if parsed_id not in seen:
                normalized_ids.append(parsed_id)
                seen.add(parsed_id)
        if len(normalized_ids) > MAX_SUB2API_ACCOUNTS:
            raise ValueError("Too many account ids were provided.")
        if not normalized_ids:
            return {}
        if not 1 <= int(days) <= SUB2API_DAILY_STATS_MAX_DAYS:
            raise ValueError(
                f"days must be between 1 and {SUB2API_DAILY_STATS_MAX_DAYS}."
            )

        config = await get_runtime_config_service().get_sub2api_config()
        semaphore = asyncio.Semaphore(SUB2API_DAILY_STATS_CONCURRENCY)

        async def fetch(account_id: int) -> tuple[int, dict[date, float]]:
            async with semaphore:
                payload = await self._request(
                    "GET",
                    f"{config.accounts_path}/{account_id}/stats",
                    config=config,
                    params={"days": int(days)},
                    total_timeout_seconds=SUB2API_TODAY_STATS_TIMEOUT_SECONDS,
                )
            return account_id, _parse_account_daily_costs(payload)

        results = await asyncio.gather(
            *(fetch(account_id) for account_id in normalized_ids),
            return_exceptions=True,
        )
        values: dict[int, dict[date, float]] = {}
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                continue
            account_id, costs = result
            if costs:
                values[account_id] = costs
        return values

    async def get_payment_balance_recharge_multiplier_info(self) -> tuple[float, bool]:
        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request("GET", "/admin/settings", config=config)
        found, raw_value = _find_nested_field(payload, "payment_balance_recharge_multiplier")
        if not found:
            return 1.0, False
        value = _bounded_number(raw_value, minimum=0.000000001, maximum=1000)
        if value is None:
            raise Sub2ApiRequestError("sub2api returned an invalid payment balance recharge multiplier.")
        return value, True

    async def get_payment_balance_recharge_multiplier(self) -> float:
        value, _field_present = await self.get_payment_balance_recharge_multiplier_info()
        return value

    async def get_account_current_rate_multiplier(self, account_id: str | int) -> float:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        account = await self.get_account_by_id(numeric_id)
        if account is None:
            raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
        value = _bounded_number(account.get("rate_multiplier"), minimum=0, maximum=1000)
        if value is None:
            raise Sub2ApiRequestError("sub2api account returned an invalid rate multiplier.")
        return value

    async def update_account_rate_multiplier(self, account_id: str | int, rate_multiplier: float) -> None:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        parsed_rate = _bounded_number(rate_multiplier, minimum=0, maximum=1000)
        if parsed_rate is None:
            raise ValueError("rate_multiplier must be between 0 and 1000.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._request(
            "POST",
            f"{config.accounts_path}/bulk-update",
            config=config,
            json={"account_ids": [numeric_id], "rate_multiplier": parsed_rate},
        )

    async def update_account_priorities(
        self,
        account_ids: list[int],
        priority: int,
    ) -> None:
        normalized_ids: list[int] = []
        seen: set[int] = set()
        for account_id in account_ids:
            parsed_id = _positive_int(account_id)
            if parsed_id is None:
                raise ValueError("Account ids must be positive integers.")
            if parsed_id not in seen:
                normalized_ids.append(parsed_id)
                seen.add(parsed_id)
        if not normalized_ids:
            raise ValueError("At least one account id is required.")
        if len(normalized_ids) > MAX_SUB2API_ACCOUNTS:
            raise ValueError("Too many account ids were provided.")
        parsed_priority = _nonnegative_int(priority)
        if parsed_priority is None:
            raise ValueError("priority must be a non-negative safe integer.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._request(
            "POST",
            f"{config.accounts_path}/bulk-update",
            config=config,
            json={"account_ids": normalized_ids, "priority": parsed_priority},
        )

    async def update_account_name(
        self,
        account_id: str | int,
        name: str,
        *,
        validate_current: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        normalized_name = str(name or "").strip()
        if not normalized_name or len(normalized_name) > 100:
            raise ValueError("name must contain between 1 and 100 characters.")

        config = await get_runtime_config_service().get_sub2api_config()
        current = await self.get_account_by_id(numeric_id, config=config)
        if current is None:
            raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
        if validate_current is not None:
            validate_current(current)

        mutation_error: Exception | None = None
        try:
            await self._request(
                "PUT",
                f"{config.accounts_path}/{numeric_id}",
                config=config,
                json={"name": normalized_name},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A lost response does not prove that the remote mutation failed.
            # Resolve the outcome with bounded GETs and never repeat the PUT.
            mutation_error = exc

        account_was_seen = False
        readback_completed = False
        try:
            async with asyncio.timeout(SUB2API_MUTATION_READBACK_TIMEOUT_SECONDS):
                for attempt in range(SUB2API_MUTATION_READBACK_ATTEMPTS):
                    try:
                        updated = await self.get_account_by_id(numeric_id, config=config)
                        readback_completed = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        updated = None
                    if updated is not None:
                        account_was_seen = True
                        if self.account_name(updated) == normalized_name:
                            return updated
                    if attempt + 1 < SUB2API_MUTATION_READBACK_ATTEMPTS:
                        await asyncio.sleep(SUB2API_MUTATION_READBACK_DELAY_SECONDS)
        except TimeoutError:
            pass

        if mutation_error is not None:
            raise mutation_error
        if readback_completed and not account_was_seen:
            raise Sub2ApiRequestError(
                "sub2api account was not found after updating its name.",
                status_code=404,
            )
        if not readback_completed:
            raise Sub2ApiRequestError("sub2api account readback did not complete after updating its name.")
        raise Sub2ApiRequestError("sub2api did not confirm the updated account name.")

    def account_notes(self, account: dict[str, Any]) -> str:
        value = account.get("notes")
        return str(value).strip() if value is not None else ""

    def account_groups(self, account: dict[str, Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        raw_groups = account.get("groups")
        if not isinstance(raw_groups, list):
            raw_groups = account.get("account_groups")
        if isinstance(raw_groups, list):
            for item in raw_groups:
                if not isinstance(item, dict):
                    continue
                group_id = str(item.get("id") or item.get("group_id") or "").strip()
                if not group_id or group_id in seen:
                    continue
                group_name = str(item.get("name") or item.get("group_name") or group_id).strip()
                result.append({"id": group_id, "name": group_name or group_id})
                seen.add(group_id)
        if result:
            return result
        raw_group_ids = account.get("group_ids")
        if isinstance(raw_group_ids, list):
            for value in raw_group_ids:
                group_id = str(value or "").strip()
                if group_id and group_id not in seen:
                    result.append({"id": group_id, "name": f"分组 #{group_id}"})
                    seen.add(group_id)
        return result

    async def update_account_notes(
        self,
        account_id: str | int,
        notes: str,
        *,
        validate_current: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        normalized_notes = str(notes or "").strip()
        if len(normalized_notes) > 10_000:
            raise ValueError("notes must not exceed 10000 characters.")

        config = await get_runtime_config_service().get_sub2api_config()
        current = await self.get_account_by_id(numeric_id, config=config)
        if current is None:
            raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
        if validate_current is not None:
            validate_current(current)

        mutation_error: Exception | None = None
        try:
            await self._request(
                "PUT",
                f"{config.accounts_path}/{numeric_id}",
                config=config,
                json={"notes": normalized_notes or None},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mutation_error = exc

        account_was_seen = False
        readback_completed = False
        try:
            async with asyncio.timeout(SUB2API_MUTATION_READBACK_TIMEOUT_SECONDS):
                for attempt in range(SUB2API_MUTATION_READBACK_ATTEMPTS):
                    try:
                        updated = await self.get_account_by_id(numeric_id, config=config)
                        readback_completed = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        updated = None
                    if updated is not None:
                        account_was_seen = True
                        if self.account_notes(updated) == normalized_notes:
                            return updated
                    if attempt + 1 < SUB2API_MUTATION_READBACK_ATTEMPTS:
                        await asyncio.sleep(SUB2API_MUTATION_READBACK_DELAY_SECONDS)
        except TimeoutError:
            pass

        if mutation_error is not None:
            raise mutation_error
        if readback_completed and not account_was_seen:
            raise Sub2ApiRequestError(
                "sub2api account was not found after updating its notes.",
                status_code=404,
            )
        if not readback_completed:
            raise Sub2ApiRequestError("sub2api account readback did not complete after updating its notes.")
        raise Sub2ApiRequestError("sub2api did not confirm the updated account notes.")

    async def update_account_base_url(
        self,
        account_id: str | int,
        base_url: str,
        *,
        validate_current: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        normalized_base_url = canonicalize_upstream_url(base_url)
        config = await get_runtime_config_service().get_sub2api_config()
        current = await self.get_account_by_id(numeric_id, config=config)
        if current is None:
            raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
        if validate_current is not None:
            validate_current(current)

        mutation_error: Exception | None = None
        try:
            # bulk-update merges this credentials patch without replacing the
            # account's API key or other provider credentials.
            await self._request(
                "POST",
                f"{config.accounts_path}/bulk-update",
                config=config,
                json={
                    "account_ids": [numeric_id],
                    "credentials": {"base_url": normalized_base_url},
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A lost response may still mean the one mutation succeeded. Read
            # back the account, but never repeat the remote write.
            mutation_error = exc

        account_was_seen = False
        readback_completed = False
        try:
            async with asyncio.timeout(SUB2API_MUTATION_READBACK_TIMEOUT_SECONDS):
                for attempt in range(SUB2API_MUTATION_READBACK_ATTEMPTS):
                    try:
                        updated = await self.get_account_by_id(numeric_id, config=config)
                        readback_completed = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        updated = None
                    if updated is not None:
                        account_was_seen = True
                        raw_base_url = _first_value(
                            updated,
                            ("credentials", "base_url"),
                            ("base_url",),
                        )
                        try:
                            confirmed_base_url = canonicalize_upstream_url(str(raw_base_url or ""))
                        except ValueError:
                            confirmed_base_url = None
                        if confirmed_base_url == normalized_base_url:
                            return updated
                    if attempt + 1 < SUB2API_MUTATION_READBACK_ATTEMPTS:
                        await asyncio.sleep(SUB2API_MUTATION_READBACK_DELAY_SECONDS)
        except TimeoutError:
            pass

        if mutation_error is not None:
            raise mutation_error
        if readback_completed and not account_was_seen:
            raise Sub2ApiRequestError(
                "sub2api account was not found after updating its upstream address.",
                status_code=404,
            )
        if not readback_completed:
            raise Sub2ApiRequestError(
                "sub2api account readback did not complete after updating its upstream address."
            )
        raise Sub2ApiRequestError("sub2api did not confirm the updated account upstream address.")

    async def set_account_schedulable(self, account_id: str | int, schedulable: bool) -> None:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        if not isinstance(schedulable, bool):
            raise ValueError("schedulable must be a boolean.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._request(
            "POST",
            f"{config.accounts_path}/{numeric_id}/schedulable",
            config=config,
            json={"schedulable": schedulable},
        )

    async def list_groups(self) -> list[dict[str, Any]]:
        config = await get_runtime_config_service().get_sub2api_config()
        return await self.list_groups_for_platform(None, config=config)

    async def list_groups_for_platform(
        self,
        platform: str | None,
        *,
        config: EffectiveSub2ApiConfig | None = None,
    ) -> list[dict[str, Any]]:
        config = config or await get_runtime_config_service().get_sub2api_config()
        params = {"platform": platform} if platform else None
        try:
            payload = await self._request("GET", "/admin/groups/all", config=config, params=params)
        except Sub2ApiRequestError as exc:
            if exc.status_code not in {404, 405}:
                raise
            payload = await self._request("GET", "/admin/groups", config=config, params=params)
        groups = self._unwrap(payload)
        if isinstance(groups, list):
            return [item for item in groups if isinstance(item, dict)]
        return []

    async def list_proxies(self) -> list[dict[str, Any]]:
        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request("GET", "/admin/proxies/all", config=config)
        proxies = self._unwrap(payload)
        if isinstance(proxies, list):
            return [item for item in proxies if isinstance(item, dict)]
        return []

    async def list_account_model_candidates(self, platform: str) -> list[dict[str, str]]:
        normalized_platform = str(platform or "").strip().lower()
        if not normalized_platform:
            raise ValueError("Cannot list sub2api model candidates without platform.")
        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request(
            "GET",
            "/admin/groups/0/models-list-candidates",
            config=config,
            params={"platform": normalized_platform},
        )
        data = self._unwrap(payload)
        raw_models = data.get("models") if isinstance(data, dict) else data
        if not isinstance(raw_models, list):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw_models:
            if isinstance(item, dict):
                model_id = str(item.get("id") or item.get("model_id") or "").strip()
                display_name = str(item.get("display_name") or item.get("name") or model_id).strip()
            else:
                model_id = str(item or "").strip()
                display_name = model_id
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            result.append({"id": model_id, "display_name": display_name or model_id})
        return result

    async def update_account_configuration(
        self,
        account_id: str | int,
        *,
        name: str,
        concurrency: int,
        priority: int,
        rate_multiplier: float,
        status: str | None,
        schedulable: bool,
        proxy_id: int | None,
        group_ids: list[int],
        model_whitelist: list[str],
        extra_patch: dict[str, Any] | None = None,
        validate_current: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        numeric_id = _positive_int(account_id)
        if numeric_id is None:
            raise ValueError("A positive numeric sub2api account id is required.")
        normalized_name = str(name or "").strip()
        if not normalized_name or len(normalized_name) > 100:
            raise ValueError("name must contain between 1 and 100 characters.")

        config = await get_runtime_config_service().get_sub2api_config()
        current = await self.get_account_by_id(numeric_id, config=config)
        if current is None:
            raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
        if validate_current is not None:
            validate_current(current)

        model_mapping = {model_id: model_id for model_id in model_whitelist}
        payload = {
            "account_ids": [numeric_id],
            "name": normalized_name,
            "concurrency": concurrency,
            "priority": priority,
            "rate_multiplier": rate_multiplier,
            "schedulable": schedulable,
            "proxy_id": proxy_id or 0,
            "group_ids": group_ids,
            "credentials": {"model_mapping": model_mapping or None},
        }
        if status is not None:
            payload["status"] = status
        if extra_patch:
            payload["extra"] = extra_patch
        mutation_error: Exception | None = None
        try:
            result = self._unwrap(
                await self._request(
                    "POST",
                    f"{config.accounts_path}/bulk-update",
                    config=config,
                    json=payload,
                )
            )
            if isinstance(result, dict):
                failed_ids = {_positive_int(value) for value in result.get("failed_ids", [])}
                if numeric_id in failed_ids:
                    raise Sub2ApiRequestError("sub2api rejected the account configuration update.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mutation_error = exc

        account_was_seen = False
        readback_completed = False
        try:
            async with asyncio.timeout(SUB2API_MUTATION_READBACK_TIMEOUT_SECONDS):
                for attempt in range(SUB2API_MUTATION_READBACK_ATTEMPTS):
                    try:
                        updated = await self.get_account_by_id(numeric_id, config=config)
                        readback_completed = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        updated = None
                    if updated is not None:
                        account_was_seen = True
                        if self._account_configuration_matches(
                            updated,
                            name=normalized_name,
                            concurrency=concurrency,
                            priority=priority,
                            rate_multiplier=rate_multiplier,
                            status=status,
                            schedulable=schedulable,
                            proxy_id=proxy_id,
                            group_ids=group_ids,
                            model_whitelist=model_whitelist,
                            extra_patch=extra_patch,
                        ):
                            return updated
                    if attempt + 1 < SUB2API_MUTATION_READBACK_ATTEMPTS:
                        await asyncio.sleep(SUB2API_MUTATION_READBACK_DELAY_SECONDS)
        except TimeoutError:
            pass

        if mutation_error is not None:
            raise mutation_error
        if readback_completed and not account_was_seen:
            raise Sub2ApiRequestError(
                "sub2api account was not found after updating its configuration.",
                status_code=404,
            )
        if not readback_completed:
            raise Sub2ApiRequestError(
                "sub2api account readback did not complete after updating its configuration."
            )
        raise Sub2ApiRequestError("sub2api did not confirm the updated account configuration.")

    def _account_configuration_matches(
        self,
        account: dict[str, Any],
        *,
        name: str,
        concurrency: int,
        priority: int,
        rate_multiplier: float,
        status: str | None,
        schedulable: bool,
        proxy_id: int | None,
        group_ids: list[int],
        model_whitelist: list[str],
        extra_patch: dict[str, Any] | None,
    ) -> bool:
        credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        mapping = credentials.get("model_mapping") if isinstance(credentials, dict) else None
        confirmed_models = sorted(
            str(key)
            for key, value in (mapping.items() if isinstance(mapping, dict) else ())
            if isinstance(value, str) and str(key) == value
        )
        confirmed_groups = sorted(
            value
            for raw in account.get("group_ids", [])
            if (value := _positive_int(raw)) is not None
        )
        confirmed_proxy = _positive_int(account.get("proxy_id"))
        confirmed_rate = _bounded_number(account.get("rate_multiplier"), minimum=0, maximum=1000)
        return (
            self.account_name(account) == name
            and _nonnegative_int(account.get("concurrency")) == concurrency
            and _nonnegative_int(account.get("priority")) == priority
            and confirmed_rate is not None
            and math.isclose(confirmed_rate, rate_multiplier, rel_tol=1e-9, abs_tol=1e-9)
            and (status is None or str(account.get("status") or "").strip().lower() == status)
            and account.get("schedulable") is schedulable
            and confirmed_proxy == proxy_id
            and confirmed_groups == sorted(group_ids)
            and confirmed_models == sorted(model_whitelist)
            and _mapping_patch_matches(extra, extra_patch or {})
        )

    async def get_account_usage(self, account: dict[str, Any] | str, force: bool = True) -> dict[str, Any]:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot read sub2api account usage without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        params = {"source": "active"}
        if force:
            params["force"] = "true"
        payload = await self._request(
            "GET",
            f"{config.accounts_path}/{account_id}/usage",
            config=config,
            params=params,
        )
        usage = self._unwrap(payload)
        return usage if isinstance(usage, dict) else {}

    async def update_access_token(self, account: dict[str, Any], access_token: str) -> None:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        token_path = config.access_token_path
        payload: dict[str, Any]
        if token_path == "credentials.access_token":
            await self._update_credentials_patch(account, {"access_token": access_token}, config)
            await self._after_account_credentials_update(account, account_id, config)
            return
        if token_path.startswith("credentials."):
            credentials = copy.deepcopy(account.get("credentials") or {})
            _deep_set({"credentials": credentials}, token_path, access_token)
            payload = {"credentials": credentials}
        else:
            payload = {}
            _deep_set(payload, token_path, access_token)

        await self._request("PUT", f"{config.accounts_path}/{account_id}", config=config, json=payload)
        await self._after_account_credentials_update(account, account_id, config)

    async def update_credentials_from_session(
        self,
        account: dict[str, Any],
        session: dict[str, Any] | None,
        access_token: str,
    ) -> list[str]:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        credentials = self.credentials_from_session(session, access_token)
        self._add_refresh_token_aliases(account, credentials)
        if not credentials.get("access_token"):
            raise ValueError("Session endpoint did not include a usable access token.")

        changes = self.changed_credentials(account, credentials)
        config = await get_runtime_config_service().get_sub2api_config()
        if changes:
            await self._update_credentials_patch(account, changes, config)
            self._merge_account_credentials(account, changes)
        if changes or self.account_requires_state_recovery(account):
            await self._after_account_credentials_update(account, account_id, config)
        return sorted(changes)

    async def reassert_subscription_state_from_session(
        self,
        account: dict[str, Any],
        session: dict[str, Any] | None,
        access_token: str,
    ) -> list[str]:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        credentials = self.subscription_credentials_from_session(session, access_token)
        if not credentials:
            return []

        config = await get_runtime_config_service().get_sub2api_config()
        await self._update_credentials_patch(account, credentials, config)
        self._merge_account_credentials(account, credentials)
        return sorted(credentials)

    async def refresh_account_usage(self, account: dict[str, Any] | str) -> bool:
        return await self.refresh_account_usage_data(account) is not None

    async def refresh_account_usage_data(
        self,
        account: dict[str, Any] | str,
        *,
        config: EffectiveSub2ApiConfig | None = None,
        force: bool = True,
    ) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot refresh sub2api account usage without id.")

        config = config or await get_runtime_config_service().get_sub2api_config()
        try:
            params = {"source": "active"}
            if force:
                params["force"] = "true"
            payload = await self._request(
                "GET",
                f"{config.accounts_path}/{account_id}/usage",
                config=config,
                params=params,
                total_timeout_seconds=SUB2API_USAGE_REFRESH_TIMEOUT_SECONDS,
            )
        except Sub2ApiRequestError as exc:
            if exc.status_code in {404, 405}:
                return None
            raise
        usage = self._unwrap(payload)
        return usage if isinstance(usage, dict) else {}

    async def check_openai_account_status(self, account: dict[str, Any] | str) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot check sub2api account status without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request(
                "POST",
                f"{config.accounts_path}/{account_id}/check-status",
                config=config,
            )
        except Sub2ApiRequestError as exc:
            if exc.status_code in {404, 405}:
                return None
            raise

        status = self._unwrap(payload)
        return status if isinstance(status, dict) else None

    async def refresh_account_credentials(self, account: dict[str, Any] | str) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot refresh sub2api account credentials without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            payload = await self._request(
                "POST",
                f"{config.accounts_path}/{account_id}/refresh",
                config=config,
            )
        except Sub2ApiRequestError as exc:
            if exc.status_code in {404, 405}:
                return None
            raise

        refreshed = self._unwrap(payload)
        if isinstance(refreshed, dict):
            if isinstance(account, dict):
                account.update(refreshed)
                await self._after_account_credentials_update(account, account_id, config)
            return refreshed
        return {}

    async def get_account(self, account: dict[str, Any] | str) -> dict[str, Any] | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        email = None if isinstance(account, str) else self.account_email(account)
        normalized_email = email.lower() if isinstance(email, str) else None
        for item in await self.list_accounts():
            item_id = self.account_id(item)
            if account_id and item_id == str(account_id):
                return item
            item_email = self.account_email(item)
            if normalized_email and item_email and item_email.lower() == normalized_email:
                return item
        return None

    async def create_account(
        self,
        *,
        email: str,
        access_token: str,
        session: dict[str, Any] | None = None,
        refresh_token: str | None = None,
        id_token: str | None = None,
        phone_number: str | None = None,
        phone_sms_url: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            raise ValueError("Cannot create sub2api account without email.")
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("Cannot create sub2api account without access_token.")

        session_payload = copy.deepcopy(session) if isinstance(session, dict) else {}
        tokens = session_payload.get("tokens") if isinstance(session_payload.get("tokens"), dict) else {}
        if refresh_token:
            session_payload["refresh_token"] = refresh_token
            tokens["refresh_token"] = refresh_token
        if id_token:
            session_payload["id_token"] = id_token
            tokens["id_token"] = id_token
        session_payload["access_token"] = token
        tokens["access_token"] = token
        session_payload["tokens"] = tokens
        session_payload.setdefault("email", normalized_email)

        credentials = self.credentials_from_session(session_payload, token)
        credentials.update(self.subscription_credentials_from_session(session_payload, token))
        credentials["email"] = normalized_email

        extra: dict[str, Any] = {"email": normalized_email}
        mail_console: dict[str, Any] = {}
        if phone_number:
            mail_console["phone_verification_phone"] = phone_number
        if phone_sms_url:
            mail_console["phone_verification_url"] = phone_sms_url
        if notes:
            mail_console["note"] = notes
        if mail_console:
            extra["mail_console"] = mail_console

        payload = {
            "name": normalized_email,
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
            "extra": extra,
            "concurrency": 2,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True,
        }

        config = await get_runtime_config_service().get_sub2api_config()
        created = await self._request("POST", config.accounts_path, config=config, json=payload)
        unwrapped = self._unwrap(created)
        return unwrapped if isinstance(unwrapped, dict) else payload

    def account_has_refresh_token(self, account: dict[str, Any]) -> bool:
        credentials_status = account.get("credentials_status")
        if isinstance(credentials_status, dict):
            for key in ("has_refresh_token", "has_refreshToken", "has_rt"):
                if credentials_status.get(key) is True:
                    return True

        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return False
        for key in ("refresh_token", "refreshToken", "rt"):
            value = credentials.get(key)
            if isinstance(value, str) and value.strip() and not _looks_redacted(value):
                return True
        return False

    async def get_account_models(self, account: dict[str, Any] | str) -> list[dict[str, str]]:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot list sub2api account models without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        payload = await self._request(
            "GET",
            f"{config.accounts_path}/{account_id}/models",
            config=config,
        )
        models = self._unwrap(payload)
        if not isinstance(models, list):
            return []

        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or item.get("model_id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            display_name = str(
                item.get("display_name")
                or item.get("displayName")
                or item.get("name")
                or model_id
            ).strip()
            result.append({"id": model_id, "display_name": display_name or model_id})
        return result

    async def test_account_connection(
        self,
        account: dict[str, Any] | str,
        model_id: str,
        *,
        prompt: str = "hi",
    ) -> tuple[bool, str | None]:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot test sub2api account without id.")
        selected_model = str(model_id or "").strip()
        if not selected_model:
            raise ValueError("Cannot test sub2api account without model id.")

        deadline = asyncio.get_running_loop().time() + SUB2API_TEST_TOTAL_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout_at(deadline):
                config = await get_runtime_config_service().get_sub2api_config()
                return await self._test_account_connection_request(
                    account_id,
                    selected_model,
                    prompt,
                    config,
                )
        except Sub2ApiRequestError:
            raise
        except TimeoutError as exc:
            raise Sub2ApiRequestError("sub2api account test request timed out.") from exc
        except httpx.HTTPError as exc:
            detail = self._redact_error_text(exc)
            raise Sub2ApiRequestError(f"sub2api account test request failed: {detail}") from exc

    async def _test_account_connection_request(
        self,
        account_id: str,
        selected_model: str,
        prompt: str,
        config: EffectiveSub2ApiConfig,
    ) -> tuple[bool, str | None]:
        url = self._url(config, f"{config.accounts_path}/{account_id}/test")
        timeout = httpx.Timeout(60.0, connect=10.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=self._headers(config),
            trust_env=False,
            transport=self.transport,
        ) as client:
            async with client.stream(
                "POST",
                url,
                json={"model_id": selected_model, "prompt": prompt, "mode": "default"},
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    _body, truncated = await _read_bounded_response(
                        response,
                        limit=MAX_SUB2API_ERROR_PREVIEW_BYTES,
                    )
                    body_state = "exceeded the diagnostic limit" if truncated else "was omitted"
                    raise Sub2ApiRequestError(
                        f"sub2api account test failed: HTTP {response.status_code}; "
                        f"remote response body {body_state}.",
                        status_code=response.status_code,
                    )

                completed: bool | None = None
                error_message: str | None = None
                async for line in _iter_bounded_response_lines(response):
                    stripped = line.strip()
                    if not stripped.startswith("data:"):
                        continue
                    event_text = stripped[5:].strip()
                    if not event_text or event_text == "[DONE]":
                        continue
                    try:
                        event = json.loads(event_text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type") or "").strip().lower()
                    if event_type == "error":
                        error_message = self._redact_error_text(
                            event.get("error") or event.get("text") or "sub2api test failed."
                        )
                    elif event_type == "test_complete":
                        completed = event.get("success") is True
                        if not completed:
                            error_message = self._redact_error_text(
                                event.get("error") or error_message or "sub2api test failed."
                            )

                if completed is True:
                    return True, None
                if error_message:
                    return False, error_message
                return False, "sub2api 测试未返回完成状态。"

    async def test_account_for_deactivation(self, account: dict[str, Any] | str) -> bool | None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot test sub2api account without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        url = self._url(config, f"{config.accounts_path}/{account_id}/test")
        async with httpx.AsyncClient(timeout=30.0, headers=self._headers(config), trust_env=False) as client:
            try:
                async with client.stream("POST", url, json={"prompt": "hi"}) as response:
                    text = ""
                    async for chunk in response.aiter_text():
                        text += chunk
                        if looks_deactive_text(text):
                            return True
                        if len(text) >= 20_000:
                            break
                    if response.status_code in {404, 405}:
                        return None
                    if response.status_code >= 400 and looks_deactive_text(text):
                        return True
                    return False
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in {404, 405}:
                    return None
                raise

    async def delete_account(self, account: dict[str, Any] | str) -> bool:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot delete sub2api account without id.")

        config = await get_runtime_config_service().get_sub2api_config()
        try:
            await self._request("DELETE", f"{config.accounts_path}/{account_id}", config=config)
            return True
        except Sub2ApiRequestError as exc:
            if exc.status_code == 404:
                return False
            raise

    def credentials_from_session(self, session: dict[str, Any] | None, fallback_access_token: str) -> dict[str, Any]:
        data: dict[str, Any] = session if isinstance(session, dict) else {}
        access_token = _first_string(
            data,
            ("accessToken",),
            ("access_token",),
            ("tokens", "accessToken"),
            ("tokens", "access_token"),
            ("token",),
        ) or fallback_access_token
        id_token = _first_string(data, ("idToken",), ("id_token",), ("tokens", "idToken"), ("tokens", "id_token"))
        refresh_token = _first_string(
            data,
            ("refreshToken",),
            ("refresh_token",),
            ("rt",),
            ("tokens", "refreshToken"),
            ("tokens", "refresh_token"),
            ("tokens", "rt"),
        )

        access_claims = _decode_jwt_payload(access_token)
        id_claims = _decode_jwt_payload(id_token)
        openai_auth = access_claims.get("https://api.openai.com/auth")
        if not isinstance(openai_auth, dict):
            openai_auth = id_claims.get("https://api.openai.com/auth")
        if not isinstance(openai_auth, dict):
            openai_auth = {}

        credentials: dict[str, Any] = {"access_token": access_token}
        _set_if_present(credentials, "refresh_token", refresh_token)
        _set_if_present(credentials, "id_token", id_token)
        _set_if_present(credentials, "client_id", _first_string(data, ("client_id",), ("clientId",), ("tokens", "client_id")))

        expires_value = _first_value(
            data,
            ("tokens", "expires_at"),
            ("tokens", "expiresAt"),
            ("expires_at",),
            ("expiresAt",),
        )
        expires_at = _coerce_rfc3339(expires_value)
        if expires_at is None and access_claims.get("exp") is not None:
            expires_at = _coerce_rfc3339(access_claims.get("exp"))
        _set_if_present(credentials, "expires_at", expires_at)

        email = (
            _first_string(data, ("email",), ("user", "email"))
            or str(access_claims.get("email") or id_claims.get("email") or "").strip()
        )
        _set_if_present(credentials, "email", email)

        _set_if_present(
            credentials,
            "chatgpt_account_id",
            _first_string(
                data,
                ("chatgpt_account_id",),
                ("chatgptAccountId",),
                ("account_id",),
                ("accountId",),
                ("account", "id"),
                ("account", "account_id"),
                ("account", "chatgpt_account_id"),
            )
            or openai_auth.get("chatgpt_account_id"),
        )
        _set_if_present(
            credentials,
            "chatgpt_user_id",
            _first_string(
                data,
                ("chatgpt_user_id",),
                ("chatgptUserId",),
                ("user_id",),
                ("userId",),
                ("user", "id"),
            )
            or openai_auth.get("chatgpt_user_id")
            or openai_auth.get("user_id")
            or access_claims.get("sub"),
        )
        _set_if_present(
            credentials,
            "organization_id",
            _first_string(
                data,
                ("organization_id",),
                ("organizationId",),
                ("org_id",),
                ("orgId",),
                ("account", "organization_id"),
                ("account", "organizationId"),
            )
            or _default_organization_id(openai_auth),
        )
        _set_if_present(credentials, "plan_type", self.session_plan_type(data, openai_auth))
        subscription_expires_at = _coerce_rfc3339(
            _first_value(
                data,
                ("subscription_expires_at",),
                ("subscriptionExpiresAt",),
                ("account", "subscriptionExpiresAt"),
                ("account", "subscription_expires_at"),
                ("account", "entitlement", "expires_at"),
                ("entitlement", "expires_at"),
                ("entitlement", "expiresAt"),
            )
        )
        subscription_renews_at = _coerce_rfc3339(
            _first_value(
                data,
                ("subscription_renews_at",),
                ("subscriptionRenewsAt",),
                ("account", "entitlement", "renews_at"),
                ("entitlement", "renews_at"),
                ("entitlement", "renewsAt"),
            )
        )
        subscription_cancels_at = _coerce_rfc3339(
            _first_value(
                data,
                ("subscription_cancels_at",),
                ("subscriptionCancelsAt",),
                ("account", "entitlement", "cancels_at"),
                ("entitlement", "cancels_at"),
                ("entitlement", "cancelsAt"),
            )
        )
        subscription_billing_period = _first_string(
            data,
            ("subscription_billing_period",),
            ("subscriptionBillingPeriod",),
            ("account", "entitlement", "billing_period"),
            ("entitlement", "billing_period"),
            ("entitlement", "billingPeriod"),
        )
        _set_if_present(
            credentials,
            "subscription_starts_at",
            _coerce_rfc3339(
                _first_value(
                    data,
                    ("subscription_starts_at",),
                    ("subscriptionStartsAt",),
                    ("subscription_started_at",),
                    ("subscriptionStartedAt",),
                    ("account", "entitlement", "starts_at"),
                    ("entitlement", "starts_at"),
                    ("entitlement", "startsAt"),
                )
            )
            or _subscription_starts_at(subscription_renews_at or subscription_cancels_at, subscription_billing_period),
        )
        _set_if_present(
            credentials,
            "subscription_expires_at",
            subscription_expires_at,
        )
        _set_if_present(
            credentials,
            "subscription_renews_at",
            subscription_renews_at,
        )
        _set_if_present(
            credentials,
            "subscription_cancels_at",
            subscription_cancels_at,
        )
        _set_if_present(
            credentials,
            "subscription_billing_period",
            subscription_billing_period,
        )
        _set_if_present(
            credentials,
            "subscription_plan",
            _first_string(
                data,
                ("subscription_plan",),
                ("subscriptionPlan",),
                ("account", "entitlement", "subscription_plan"),
                ("entitlement", "subscription_plan"),
                ("entitlement", "subscriptionPlan"),
            ),
        )
        _set_if_present(
            credentials,
            "has_active_subscription",
            _first_value(
                data,
                ("has_active_subscription",),
                ("hasActiveSubscription",),
                ("account", "entitlement", "has_active_subscription"),
                ("entitlement", "has_active_subscription"),
                ("entitlement", "hasActiveSubscription"),
            ),
        )
        return credentials

    def session_plan_type(self, session: dict[str, Any], openai_auth: dict[str, Any] | None = None) -> str | None:
        plan_type = _first_string(
            session,
            ("account", "planType"),
            ("account", "plan_type"),
            ("planType",),
            ("plan_type",),
            ("entitlement", "subscription_plan"),
            ("entitlement", "subscriptionPlan"),
        )
        if plan_type:
            return plan_type

        claim_plan_type = (openai_auth or {}).get("chatgpt_plan_type")
        if isinstance(claim_plan_type, str):
            text = claim_plan_type.strip()
            return text or None
        return None

    def subscription_credentials_from_session(
        self,
        session: dict[str, Any] | None,
        access_token: str,
    ) -> dict[str, Any]:
        credentials = self.credentials_from_session(session, access_token)
        return {
            key: credentials[key]
            for key in SUBSCRIPTION_CREDENTIAL_KEYS
            if credentials.get(key) not in (None, "")
        }

    def changed_credentials(self, account: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        current = account.get("credentials")
        if not isinstance(current, dict):
            current = {}
        changes: dict[str, Any] = {}
        for key, value in credentials.items():
            if value in (None, ""):
                continue
            current_value = current.get(key)
            if key not in current:
                changes[key] = value
                continue
            if key in SENSITIVE_CREDENTIAL_KEYS and _looks_redacted(current_value):
                changes[key] = value
                continue
            if not _credential_values_equal(key, current_value, value):
                changes[key] = value
        return changes

    def _merge_account_credentials(self, account: dict[str, Any], credentials: dict[str, Any]) -> None:
        current = account.get("credentials")
        if not isinstance(current, dict):
            current = {}
            account["credentials"] = current
        current.update(credentials)

    def _add_refresh_token_aliases(self, account: dict[str, Any], credentials: dict[str, Any]) -> None:
        refresh_token = credentials.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return
        current = account.get("credentials")
        if not isinstance(current, dict):
            current = {}
        credentials_status = account.get("credentials_status")
        has_rt = isinstance(credentials_status, dict) and credentials_status.get("has_rt") is True
        has_refresh_token_status = (
            isinstance(credentials_status, dict)
            and credentials_status.get("has_refresh_token") is True
            and "refresh_token" not in current
            and "refreshToken" not in current
        )
        if "rt" in current or has_rt or has_refresh_token_status:
            credentials["rt"] = refresh_token

    async def _update_credentials_patch(
        self,
        account: dict[str, Any],
        credentials: dict[str, Any],
        config: EffectiveSub2ApiConfig,
    ) -> None:
        account_id = self.account_id(account)
        if account_id is None:
            raise ValueError("Cannot update sub2api account without id.")

        numeric_id = self._numeric_account_id(account_id)
        if numeric_id is not None:
            try:
                await self._request(
                    "POST",
                    f"{config.accounts_path}/bulk-update",
                    config=config,
                    json={"account_ids": [numeric_id], "credentials": credentials},
                )
                return
            except Sub2ApiRequestError as exc:
                if exc.status_code not in {404, 405}:
                    raise

        merged_credentials = copy.deepcopy(account.get("credentials") or {})
        merged_credentials.update(credentials)
        await self._request(
            "PUT",
            f"{config.accounts_path}/{account_id}",
            config=config,
            json={"credentials": merged_credentials},
        )

    async def _after_account_credentials_update(
        self,
        account: dict[str, Any],
        account_id: str,
        config: EffectiveSub2ApiConfig,
    ) -> None:
        should_recover_state = self.account_requires_state_recovery(account)
        if should_recover_state:
            await self._recover_account_state(account_id, config)

    async def recover_account_state(self, account: dict[str, Any] | str) -> None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot recover sub2api account state without id.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._recover_account_state(account_id, config)

    async def clear_rate_limit_state(self, account: dict[str, Any] | str) -> None:
        account_id = account if isinstance(account, str) else self.account_id(account)
        if not account_id:
            raise ValueError("Cannot clear sub2api rate limit state without id.")
        config = await get_runtime_config_service().get_sub2api_config()
        await self._request(
            "PUT",
            f"{config.accounts_path}/{account_id}",
            config=config,
            json={
                "rate_limited_at": None,
                "rate_limit_reset_at": None,
                "temp_unschedulable_until": None,
                "temp_unschedulable_reason": "",
            },
        )

    async def _recover_account_state(self, account_id: str, config: EffectiveSub2ApiConfig) -> None:
        if config.auto_clear_error:
            await self._try_post(f"{config.accounts_path}/{account_id}/clear-error", config)
        if config.auto_recover_state:
            await self._try_post(f"{config.accounts_path}/{account_id}/recover-state", config)
            await self._try_post(f"{config.accounts_path}/{account_id}/schedulable", config, json={"schedulable": True})
            await self._try_delete(f"{config.accounts_path}/{account_id}/temp-unschedulable", config)

    async def _try_post(self, path: str, config: EffectiveSub2ApiConfig, **kwargs: Any) -> None:
        try:
            await self._request("POST", path, config=config, **kwargs)
        except Sub2ApiRequestError as exc:
            if exc.status_code not in {404, 405}:
                raise

    async def _try_delete(self, path: str, config: EffectiveSub2ApiConfig, **kwargs: Any) -> None:
        try:
            await self._request("DELETE", path, config=config, **kwargs)
        except Sub2ApiRequestError as exc:
            if exc.status_code not in {404, 405}:
                raise

    def _numeric_account_id(self, account_id: str) -> int | None:
        try:
            return int(account_id)
        except (TypeError, ValueError):
            return None

    def account_id(self, account: dict[str, Any]) -> str | None:
        for key in ("id", "account_id", "accountId"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def dedupe_accounts_by_email(
        self,
        accounts: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        metadata = self.account_duplicate_metadata(accounts)
        duplicates: list[dict[str, Any]] = []
        keep_indexes: set[int] = set()
        for index, account in enumerate(accounts):
            item = metadata.get(index)
            if item is None or item["duplicate_primary"]:
                keep_indexes.add(index)
            if item is not None and item["is_duplicate"] and not item["duplicate_primary"]:
                duplicates.append(
                    {
                        "email": item["email"],
                        "kept_account_id": item["duplicate_primary_account_id"],
                        "ignored_account_id": self.account_id(account),
                        "ignored_status": self.account_status(account),
                        "ignored_schedulable": self.account_schedulable(account),
                        "ignored_error": self.is_error_account(account),
                        "ignored_deactive": self.is_deactive_account(account),
                    }
                )

        deduped = [account for index, account in enumerate(accounts) if index in keep_indexes]
        return deduped, duplicates

    def account_duplicate_metadata(self, accounts: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, account in enumerate(accounts):
            email = self.account_email(account)
            if email:
                grouped.setdefault(email.lower(), []).append((index, account))

        metadata: dict[int, dict[str, Any]] = {}
        for email, entries in grouped.items():
            ordered = sorted(entries, key=lambda item: self._dedupe_account_sort_key(item[1], item[0]))
            primary_index, primary_account = ordered[0]
            primary_id = self.account_id(primary_account)
            group_size = len(ordered)
            for rank, (index, account) in enumerate(ordered):
                metadata[index] = {
                    "email": email,
                    "is_duplicate": group_size > 1,
                    "duplicate_group_size": group_size,
                    "duplicate_rank": rank,
                    "duplicate_primary": index == primary_index,
                    "duplicate_primary_account_id": primary_id,
                }
        return metadata

    def _dedupe_account_sort_key(self, account: dict[str, Any], original_index: int) -> tuple[int, int, int, int, int, int]:
        status = (self.account_status(account) or "").strip().lower()
        schedulable = self.account_schedulable(account)
        schedulable_rank = 0 if schedulable is True else 2 if schedulable is False else 1
        credential_rank = 0 if self.account_has_refresh_token(account) else 1 if self.account_access_token(account) else 2
        return (
            1 if self.is_deactive_account(account) else 0,
            1 if self.is_error_account(account) else 0,
            schedulable_rank,
            0 if status in HEALTHY_ACCOUNT_STATUSES else 1,
            credential_rank,
            original_index,
        )

    def account_email(self, account: dict[str, Any]) -> str | None:
        for path in ACCOUNT_EMAIL_PATHS:
            email = extract_email(_path_get(account, path))
            if email:
                return email
        return None

    def account_name(self, account: dict[str, Any]) -> str | None:
        for path in ACCOUNT_NAME_PATHS:
            value = _path_get(account, path)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def account_status(self, account: dict[str, Any]) -> str | None:
        for key in ("status", "state", "account_status"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def account_last_used_at(self, account: dict[str, Any]) -> datetime | None:
        """Return the upstream's own last-use timestamp for this account.

        sub2api reports ``last_used_at`` as an offset-aware ISO 8601 string. It
        is authoritative, so it is read rather than inferred locally. Aliases
        cover deployments that expose the field under a camelCase spelling.
        """

        for key in ("last_used_at", "lastUsedAt", "last_used_time"):
            value = account.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return None

    def account_platform(self, account: dict[str, Any]) -> str | None:
        credentials = account.get("credentials")
        for source in (account, credentials):
            if not isinstance(source, dict):
                continue
            for key in ("platform", "provider", "service"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def account_type(self, account: dict[str, Any]) -> str | None:
        for key in ("type", "account_type", "auth_type"):
            value = account.get(key)
            if value is not None:
                return str(value)
        return None

    def account_has_api_key(self, account: dict[str, Any]) -> bool:
        credentials_status = account.get("credentials_status")
        if isinstance(credentials_status, dict) and any(
            credentials_status.get(key) is True for key in ("has_api_key", "hasApiKey")
        ):
            return True
        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return False
        return any(
            isinstance(credentials.get(key), str) and bool(credentials[key].strip())
            for key in ("api_key", "apiKey", "apikey")
        )

    def is_oauth_account(self, account: dict[str, Any]) -> bool:
        account_type = (self.account_type(account) or "").strip().lower()
        normalized_type = account_type.replace("-", "_").replace(" ", "_")
        if normalized_type:
            if "oauth" in normalized_type:
                return True
            if "api_key" in normalized_type or "apikey" in normalized_type:
                return False

        credentials_status = account.get("credentials_status")
        if isinstance(credentials_status, dict):
            if any(credentials_status.get(key) is True for key in ("has_refresh_token", "has_refreshToken", "has_rt")):
                return True
            if any(credentials_status.get(key) is True for key in ("has_api_key", "hasApiKey")):
                return False

        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return False
        if any(key in credentials for key in ("refresh_token", "refreshToken", "rt", "id_token", "idToken")):
            return True
        if any(key in credentials for key in ("api_key", "apiKey", "apikey")):
            return False
        return False

    def account_schedulable(self, account: dict[str, Any]) -> bool | None:
        value = account.get("schedulable")
        if isinstance(value, bool):
            return value
        return None

    def account_error_message(self, account: dict[str, Any]) -> str | None:
        for value in (
            account.get("error_message"),
            account.get("errorMessage"),
            _deep_get(account, "error.message"),
        ):
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return self._redact_error_text(
                    text,
                    limit=MAX_REMOTE_ACCOUNT_ERROR_CHARS,
                ) or None
        return None

    def account_priority(self, account: dict[str, Any]) -> int | None:
        return _nonnegative_int(account.get("priority"))

    def account_error_status_code(self, account: dict[str, Any]) -> int | None:
        explicit = _first_value(
            account,
            ("status_code",),
            ("statusCode",),
            ("http_status",),
            ("httpStatus",),
            ("error", "status"),
            ("error", "status_code"),
            ("error", "statusCode"),
            ("error_code",),
            ("errorCode",),
            ("last_error_code",),
            ("lastErrorCode",),
        )
        parsed = _positive_int(explicit)
        if parsed is not None and 100 <= parsed <= 599:
            return parsed

        message = self.account_error_message(account)
        if not message:
            return None
        for pattern in (
            r"\((\d{3})\)",
            r"\bHTTP\s+(\d{3})\b",
            r"\bstatus(?:\s+code)?[\s\"':=]+(\d{3})\b",
        ):
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if match:
                parsed = int(match.group(1))
                if 100 <= parsed <= 599:
                    return parsed
        return None

    def account_phone_note_text(self, account: dict[str, Any]) -> str:
        phone_number = _first_string(
            account,
            ("extra", "mail_console", "phone_verification_phone"),
            ("extra", "phone_verification_phone"),
            ("phone_verification_phone",),
            ("phone_number",),
            ("extra", "phone_number"),
        )
        phone_url = _first_string(
            account,
            ("extra", "mail_console", "phone_verification_url"),
            ("extra", "phone_verification_url"),
            ("phone_verification_url",),
            ("sms_url",),
            ("extra", "sms_url"),
        )
        phone_cdk = _first_string(
            account,
            ("extra", "mail_console", "phone_verification_cdk"),
            ("extra", "phone_verification_cdk"),
            ("phone_verification_cdk",),
            ("sms_cdk",),
            ("extra", "sms_cdk"),
        )
        if phone_number and phone_cdk and phone_url:
            return f"{phone_number}----{phone_cdk}----{phone_url}"
        if phone_number and phone_cdk:
            return f"{phone_number}----{phone_cdk}"
        if phone_number and phone_url:
            return f"{phone_number}----{phone_url}"

        notes: list[str] = []
        for value in (
            account.get("notes"),
            account.get("note"),
            account.get("remark"),
            account.get("remarks"),
            account.get("description"),
            _path_get(account, ("extra", "note")),
            _path_get(account, ("extra", "notes")),
            _path_get(account, ("extra", "remark")),
            _path_get(account, ("extra", "remarks")),
            _path_get(account, ("extra", "mail_console", "note")),
        ):
            if value is None:
                continue
            text = str(value).strip()
            if text and text not in notes:
                notes.append(text)
        return "\n".join(notes)

    def account_access_token(self, account: dict[str, Any]) -> str | None:
        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return None
        token = credentials.get("access_token") or credentials.get("accessToken")
        if not isinstance(token, str):
            return None
        token = token.strip()
        if not token or _looks_redacted(token):
            return None
        return token

    def is_gpt_account(self, account: dict[str, Any]) -> bool:
        text = " ".join(
            str(item).lower()
            for item in [
                self.account_platform(account),
                self.account_type(account),
                _deep_get(account, "credentials.provider"),
                _deep_get(account, "credentials.platform"),
            ]
            if item is not None
        )
        if any(marker in text for marker in ("openai", "chatgpt", "gpt")):
            return True
        credentials = account.get("credentials")
        return isinstance(credentials, dict) and any(
            key in credentials for key in ("access_token", "accessToken", "refresh_token", "refreshToken", "rt")
        )

    def is_error_account(self, account: dict[str, Any]) -> bool:
        status = (self.account_status(account) or "").strip().lower()
        if any(marker in status for marker in ("error", "fail", "invalid", "expired", "disabled")):
            return True
        schedulable = self.account_schedulable(account)
        if schedulable is not False or "deactive" in status:
            return False
        if status in HEALTHY_ACCOUNT_STATUSES and not self.account_error_message(account):
            return False
        return True

    def account_looks_healthy(self, account: dict[str, Any]) -> bool:
        if self.is_error_account(account) or self.is_deactive_account(account):
            return False
        status = (self.account_status(account) or "").strip().lower()
        schedulable = self.account_schedulable(account)
        return schedulable is True or status in HEALTHY_ACCOUNT_STATUSES

    def account_requires_state_recovery(self, account: dict[str, Any]) -> bool:
        return not self.account_looks_healthy(account)

    def account_has_stale_rate_limit_state(self, account: dict[str, Any]) -> bool:
        return any(
            _first_string(account, path) is not None
            for path in (
                ("rate_limited_at",),
                ("rate_limit_reset_at",),
                ("temp_unschedulable_until",),
                ("temp_unschedulable_reason",),
                ("extra", "rate_limited_at"),
                ("extra", "rate_limit_reset_at"),
            )
        )

    def is_deactive_account(self, account: dict[str, Any]) -> bool:
        for key in ("deactive", "deactivated", "account_deactive"):
            if account.get(key) is True:
                return True

        status = (self.account_status(account) or "").strip().lower()
        if "deactive" in status or "deactivated" in status:
            return True

        schedulable = self.account_schedulable(account)
        if ("error" in status or "fail" in status or schedulable is False) and looks_deactive_text(
            account.get("error_message")
        ):
            return True

        explicit_code = _first_string(
            account,
            ("error_code",),
            ("errorCode",),
            ("last_error_code",),
            ("lastErrorCode",),
            ("error", "code"),
        )
        if explicit_code and explicit_code.strip().lower() in {"account_deactive", "account_deactivated"}:
            return True

        return False
