from __future__ import annotations

import re
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response


_SENSITIVE_FIELD_NAMES = {
    "apiendpointurl",
    "apikey",
    "baseurl",
    "managementbaseurl",
    "managementurl",
}
_SENSITIVE_FIELD_MARKERS = (
    "adminkey",
    "apikey",
    "authorization",
    "cookie",
    "encryptionkey",
    "password",
    "privatekey",
    "secret",
    "token",
)


def _normalized_field_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _is_sensitive_field_name(value: Any) -> bool:
    normalized = _normalized_field_name(value)
    return (
        normalized in _SENSITIVE_FIELD_NAMES
        or normalized.endswith("baseurl")
        or any(marker in normalized for marker in _SENSITIVE_FIELD_MARKERS)
    )


def _scrub_sensitive_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _is_sensitive_field_name(key) else _scrub_sensitive_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_sensitive_keys(item) for item in value]
    return value


async def sanitized_request_validation_handler(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    """Preserve validation details while redacting credential-shaped fields."""

    del request

    errors: list[dict[str, Any]] = []
    for original in exc.errors():
        error = dict(original)
        location = error.get("loc", ())
        if any(_is_sensitive_field_name(part) for part in location):
            if "input" in error:
                error["input"] = "[redacted]"
            error.pop("ctx", None)
        elif "input" in error:
            error["input"] = _scrub_sensitive_keys(error["input"])
        errors.append(error)
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(errors)},
    )


__all__ = ["sanitized_request_validation_handler"]
