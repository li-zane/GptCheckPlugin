from __future__ import annotations

from ipaddress import ip_address
import re
from urllib.parse import urlsplit, urlunsplit


SUB2API_API_PREFIX = "/api/v1"
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def normalize_management_site_base_url(value: str) -> str:
    """Validate and canonicalize the management site's Sub2API base URL."""

    if not isinstance(value, str):
        raise TypeError("sub2api base URL must be a string")
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("sub2api base URL is required")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("sub2api base URL contains invalid characters")
    if "\\" in raw:
        raise ValueError("sub2api base URL has an invalid authority")
    if "://" not in raw:
        raw = f"http://{raw}"

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("sub2api base URL has an invalid authority") from exc

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("sub2api base URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("sub2api base URL must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("sub2api base URL must not include a query or fragment")

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("sub2api base URL has an invalid authority") from exc
    if not hostname:
        raise ValueError("sub2api base URL host is required")
    if parsed.netloc.endswith(":"):
        raise ValueError("sub2api base URL has an invalid authority")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("sub2api base URL port must be between 1 and 65535")

    ascii_hostname = _canonical_hostname(hostname)
    if scheme == "http" and not is_strict_loopback_hostname(ascii_hostname):
        raise ValueError("HTTP is only allowed for a loopback sub2api address; use HTTPS")

    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    host_for_url = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    netloc = f"{host_for_url}:{port}" if port is not None else host_for_url
    instance_path = _strip_api_prefix(parsed.path)
    return urlunsplit(
        (scheme, netloc, f"{instance_path}{SUB2API_API_PREFIX}", "", "")
    )


def replace_sub2api_port(base_url: str, port: int) -> str:
    if not 1 <= int(port) <= 65535:
        raise ValueError("sub2api port must be between 1 and 65535")
    parsed = urlsplit(normalize_management_site_base_url(base_url))
    hostname = parsed.hostname
    if hostname is None:  # pragma: no cover - guarded by normalization
        raise ValueError("sub2api base URL host is required")
    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    updated = urlunsplit(
        (parsed.scheme, f"{host_for_url}:{int(port)}", parsed.path, "", "")
    )
    return normalize_management_site_base_url(updated)


def is_strict_loopback_hostname(hostname: str) -> bool:
    normalized = hostname.strip().casefold()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_loopback_sub2api_url(value: str) -> bool:
    try:
        hostname = urlsplit(normalize_management_site_base_url(value)).hostname
    except (TypeError, ValueError):
        return False
    return bool(hostname and is_strict_loopback_hostname(hostname))


def _canonical_hostname(hostname: str) -> str:
    candidate = hostname.casefold()
    if "%" in candidate:
        raise ValueError("sub2api base URL has an invalid host")
    try:
        return ip_address(candidate).compressed.casefold()
    except ValueError:
        pass

    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("sub2api base URL has an invalid host") from exc
    if len(ascii_hostname) > 253:
        raise ValueError("sub2api base URL host is too long")
    labels = ascii_hostname.split(".")
    if not labels or any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels):
        raise ValueError("sub2api base URL has an invalid host")
    return ascii_hostname


def _strip_api_prefix(path: str) -> str:
    clean_path = (path or "").strip().rstrip("/")
    if not clean_path or clean_path == "/":
        return ""
    if clean_path.casefold() == SUB2API_API_PREFIX:
        return ""
    if clean_path.casefold().endswith(SUB2API_API_PREFIX):
        return clean_path[: -len(SUB2API_API_PREFIX)].rstrip("/")
    return clean_path


__all__ = [
    "SUB2API_API_PREFIX",
    "is_loopback_sub2api_url",
    "is_strict_loopback_hostname",
    "normalize_management_site_base_url",
    "replace_sub2api_port",
]
