from __future__ import annotations

import unittest

from app.services.sub2api import Sub2ApiClient


class Sub2ApiAccountIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Sub2ApiClient()

    def test_account_name_prefers_sub2api_top_level_name(self) -> None:
        account = {
            "name": "School Account 12",
            "profile": {"name": "Profile Name", "email": "student@example.edu"},
        }

        self.assertEqual(self.client.account_name(account), "School Account 12")
        self.assertEqual(self.client.account_email(account), "student@example.edu")

    def test_account_name_trims_and_supports_known_fallback_paths(self) -> None:
        self.assertEqual(self.client.account_name({"account_name": "  OAuth Account  "}), "OAuth Account")
        self.assertEqual(self.client.account_name({"profile": {"name": "Student Profile"}}), "Student Profile")

    def test_account_name_does_not_fall_back_to_email_implicitly(self) -> None:
        account = {"credentials": {"email": "student@example.edu"}}

        self.assertIsNone(self.client.account_name(account))
        self.assertEqual(self.client.account_email(account), "student@example.edu")

    def test_account_platform_supports_nested_credential_metadata(self) -> None:
        self.assertEqual(
            self.client.account_platform({"credentials": {"provider": "gemini"}}),
            "gemini",
        )
        self.assertEqual(
            self.client.account_platform(
                {"platform": "anthropic", "credentials": {"platform": "openai"}}
            ),
            "anthropic",
        )

    def test_account_platform_ignores_structured_metadata_and_blank_values(self) -> None:
        self.assertEqual(
            self.client.account_platform(
                {"platform": " ", "credentials": {"provider": " gemini "}}
            ),
            "gemini",
        )
        self.assertIsNone(
            self.client.account_platform(
                {"credentials": {"provider": {"api_key": "synthetic-marker"}}}
            )
        )

    def test_oauth_detection_excludes_api_key_accounts(self) -> None:
        oauth = {
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "oauth@example.com", "refresh_token": "redacted"},
        }
        api_key = {
            "platform": "openai",
            "type": "api_key",
            "credentials": {"email": "key@example.com", "api_key": "redacted"},
        }

        self.assertTrue(self.client.is_gpt_account(oauth))
        self.assertTrue(self.client.is_oauth_account(oauth))
        self.assertTrue(self.client.is_gpt_account(api_key))
        self.assertFalse(self.client.is_oauth_account(api_key))


if __name__ == "__main__":
    unittest.main()
