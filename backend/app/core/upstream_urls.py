from urllib.parse import urlsplit, urlunsplit


def canonicalize_upstream_url(value: str) -> str:
    """Return the canonical site URL shared by upstream channel records."""

    if not isinstance(value, str):
        raise TypeError("URL must be a string")
    raw = value.strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("invalid URL")

    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    if scheme != "https":
        raise ValueError("Upstream URLs must use HTTPS")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("URL must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("URL must not include a query or fragment")
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
    if not ascii_hostname or any(char.isspace() for char in ascii_hostname):
        raise ValueError("invalid URL host")

    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    host_for_url = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    netloc = f"{host_for_url}:{port}" if port is not None else host_for_url

    path = parsed.path.rstrip("/")
    lowered_path = path.casefold()
    if lowered_path.endswith("/api/v1"):
        path = path[: -len("/api/v1")].rstrip("/")
    elif lowered_path.endswith("/v1"):
        path = path[: -len("/v1")].rstrip("/")
    if path == "/":
        path = ""

    return urlunsplit((scheme, netloc, path, "", ""))


def upstream_url_origin(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    parsed = urlsplit(canonicalize_upstream_url(value))
    return parsed.scheme, parsed.netloc
