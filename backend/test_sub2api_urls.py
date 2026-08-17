from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.core.sub2api_urls import (
    is_loopback_sub2api_url,
    normalize_management_site_base_url,
    replace_sub2api_port,
)
from app.schemas import AppSettingsUpdate


class Sub2ApiUrlTests(unittest.TestCase):
    def test_canonicalizes_loopback_http_and_remote_https(self) -> None:
        cases = {
            "127.0.0.1:8080": "http://127.0.0.1:8080/api/v1",
            "HTTP://LOCALHOST:8080/api/v1/": "http://localhost:8080/api/v1",
            "http://[::1]:8000": "http://[::1]:8000/api/v1",
            "https://EXAMPLE.com:443/prefix/api/v1/": "https://example.com/prefix/api/v1",
            "https://example.com/sub2api": "https://example.com/sub2api/api/v1",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_management_site_base_url(raw), expected)

    def test_only_strict_loopback_addresses_may_use_http(self) -> None:
        allowed = (
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://127.255.255.254:8080",
            "http://[::1]:8080",
        )
        for value in allowed:
            with self.subTest(value=value):
                self.assertTrue(is_loopback_sub2api_url(value))

        rejected = (
            "http://example.com:8080",
            "http://localhost.example:8080",
            "http://127.1:8080",
            "http://2130706433:8080",
            "http://127.0.0.1.example:8080",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_management_site_base_url(value)

    def test_rejects_credentials_non_http_schemes_and_invalid_authorities(self) -> None:
        rejected = (
            "ftp://example.com",
            "file:///private/path",
            "https://user:password@example.com",
            "https://example.com?token=private",
            "https://example.com/#private",
            "https://example.com:",
            "https://example.com:0",
            "https://example.com:99999",
            "https://exa_mple.com",
            "https://example..com",
            "https://[::1",
            "https://[fe80::1%25eth0]",
            "https:///missing-host",
            "https://example.com\\@evil.example",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_management_site_base_url(value)

    def test_runtime_schema_and_port_replacement_share_the_policy(self) -> None:
        payload = AppSettingsUpdate(management_site_base_url="127.0.0.1:8001")
        self.assertEqual(payload.management_site_base_url, "http://127.0.0.1:8001/api/v1")
        self.assertEqual(
            replace_sub2api_port("https://example.com/prefix", 8443),
            "https://example.com:8443/prefix/api/v1",
        )
        with self.assertRaises(ValidationError):
            AppSettingsUpdate(management_site_base_url="http://example.com:8001")


if __name__ == "__main__":
    unittest.main()
