from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.validation import sanitized_request_validation_handler
from app.schemas import AppSettingsUpdate


class _NestedPayload(BaseModel):
    value: int


class ValidationRedactionTests(unittest.TestCase):
    def test_normalizes_case_camel_case_and_separators_for_sensitive_keys(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)

        @app.post("/unrelated/redaction-test")
        async def validate(payload: _NestedPayload) -> _NestedPayload:
            return payload

        sensitive_values = {
            "AccessToken": "private-access-token-value",
            "refresh-token": "private-refresh-token-value",
            "client.secret": "private-client-secret-value",
            "DB Password": "private-password-value",
            "session_cookie": "private-cookie-value",
            "AUTHORIZATION": "private-authorization-value",
            "apiKey": "private-api-key-value",
            "x-api-key": "private-x-api-key-value",
            "APP_ADMIN_KEY": "private-admin-key-value",
            "encryption_key": "private-encryption-key-value",
            "privateKey": "private-key-value",
            "managementBaseURL": "private-url-value",
        }
        with TestClient(app) as client:
            response = client.post(
                "/unrelated/redaction-test",
                json={"value": sensitive_values},
            )

        self.assertEqual(response.status_code, 422)
        for secret in sensitive_values.values():
            self.assertNotIn(secret, response.text)
        self.assertGreaterEqual(response.text.count("[redacted]"), len(sensitive_values))

    def test_settings_url_validation_does_not_reflect_userinfo(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)

        @app.put("/api/settings")
        async def update_settings(payload: AppSettingsUpdate) -> AppSettingsUpdate:
            return payload

        secret = "private-url-password"
        with TestClient(app) as client:
            response = client.put(
                "/api/settings",
                json={"sub2api_base_url": f"https://user:{secret}@example.com"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertIn("[redacted]", response.text)


if __name__ == "__main__":
    unittest.main()
