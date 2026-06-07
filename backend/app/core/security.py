import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Cookie, HTTPException, Request, Response, status

from app.core.config import get_settings


def _sign(payload: bytes) -> str:
    secret = get_settings().app_session_secret.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _encode(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("utf-8")
    return f"{payload}.{_sign(payload.encode('utf-8'))}"


def _decode(token: str) -> dict[str, Any] | None:
    if "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    expected = _sign(payload.encode("utf-8"))
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def verify_admin_key(candidate: str) -> bool:
    expected = get_settings().app_admin_key
    return bool(candidate) and hmac.compare_digest(candidate, expected)


def _request_is_secure(request: Request | None) -> bool:
    if request is None:
        return False
    proto = str(request.headers.get("x-forwarded-proto") or "").strip().lower()
    if proto:
        return proto == "https"
    return str(request.url.scheme).lower() == "https"


def issue_session(response: Response, request: Request | None = None) -> None:
    settings = get_settings()
    now = int(time.time())
    token = _encode(
        {
            "sub": "admin",
            "iat": now,
            "exp": now + settings.app_session_ttl_seconds,
            "nonce": secrets.token_urlsafe(16),
        }
    )
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.app_session_ttl_seconds,
        httponly=True,
        secure=bool(settings.cookie_secure and _request_is_secure(request)),
        samesite="strict",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(get_settings().session_cookie_name, path="/")


async def require_admin(
    request: Request,
    session_cookie: str | None = Cookie(default=None, alias=get_settings().session_cookie_name),
) -> dict[str, Any]:
    if session_cookie:
        payload = _decode(session_cookie)
        if payload:
            return payload
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
    )
