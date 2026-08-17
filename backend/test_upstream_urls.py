import unittest

from app.core.upstream_urls import canonicalize_upstream_url
from app.schemas import ApiAccountUpdate, UpstreamUpdate


class UpstreamURLTests(unittest.TestCase):
    def test_canonicalizes_site_identity(self) -> None:
        cases = {
            "https://Example.COM:443/team/V1/": "https://example.com/team",
            "https://example.com/team/v1/v1": "https://example.com/team/v1",
            "https://example.com:8443/team/v10/": "https://example.com:8443/team/v10",
            "https://[2001:db8::1]:443/v1": "https://[2001:db8::1]",
            "https://ex\u00e4mple.com/v1": "https://xn--exmple-cua.com",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonicalize_upstream_url(raw), expected)

    def test_rejects_unsafe_or_non_http_urls(self) -> None:
        invalid_values = (
            "",
            "http://example.com/v1",
            "ftp://example.com/v1",
            "https://user:secret@example.com/v1",
            "https://example.com/v1?token=secret",
            "https://example.com/v1#secret",
            "https://exa mple.com/v1",
            "https://example.com:invalid/v1",
            "https://example.com/\x00v1",
        )
        for raw in invalid_values:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                canonicalize_upstream_url(raw)

    def test_account_and_channel_inputs_share_canonicalization(self) -> None:
        expected = "https://example.com/prefix"
        self.assertEqual(
            ApiAccountUpdate(
                expected_identity_fingerprint="0" * 64,
                api_endpoint_url="https://EXAMPLE.com:443/prefix/v1/",
            ).api_endpoint_url,
            expected,
        )
        self.assertEqual(
            UpstreamUpdate(
                api_endpoint_url="https://EXAMPLE.com:443/prefix/api/v1/"
            ).api_endpoint_url,
            expected,
        )


if __name__ == "__main__":
    unittest.main()
