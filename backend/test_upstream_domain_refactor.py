import unittest

from app.main import app
from app.models import ApiAccount, Upstream
from app.schemas import UpstreamOut, UpstreamUsageHistoryDayOut


FORBIDDEN_PUBLIC_FIELDS = {
    "channel_id",
    "channel_identity",
    "channel_name",
    "sub2api_account_id",
    "sub2api_cost",
    "sub2api_cost_cny",
    "stable_id",
}


class UpstreamDomainContractTests(unittest.TestCase):
    def test_domain_orm_uses_only_canonical_field_names(self) -> None:
        upstream_columns = set(Upstream.__table__.columns.keys())
        account_columns = set(ApiAccount.__table__.columns.keys())

        self.assertTrue(
            {
                "api_endpoint_url",
                "management_url",
                "platform_type",
                "upstream_recharge_multiplier_override",
            }.issubset(upstream_columns)
        )
        self.assertTrue(
            {
                "management_account_id",
                "upstream_id",
                "remote_upstream_api_key_id",
                "api_endpoint_url",
                "platform_type",
                "upstream_group_multiplier_override",
                "upstream_recharge_multiplier_override",
            }.issubset(account_columns)
        )
        forbidden = {
            "canonical_base_url",
            "management_base_url",
            "upstream_type",
            "manual_group_multiplier",
            "manual_recharge_multiplier",
            "upstream_api_key_record_id",
        }
        self.assertFalse(upstream_columns & forbidden)
        self.assertFalse(account_columns & forbidden)

    def test_upstream_public_routes_use_only_new_contract_paths(self) -> None:
        paths = set(app.openapi()["paths"])

        self.assertIn("/api/upstreams", paths)
        self.assertIn("/api/api-accounts", paths)
        self.assertIn("/api/api-accounts/{management_account_id}", paths)
        self.assertIn("/api/api-accounts/upstream-change-events", paths)
        self.assertNotIn("/api/upstream-channels", paths)
        self.assertNotIn("/api/upstream-accounts", paths)
        self.assertFalse(any("channel-monitors" in path for path in paths))

    def test_upstream_public_schemas_exclude_legacy_identity_and_money_fields(self) -> None:
        schemas = app.openapi()["components"]["schemas"]
        relevant = {
            name: schema
            for name, schema in schemas.items()
            if name.startswith("Upstream") or name.startswith("AccountScheduling")
        }

        for name, schema in relevant.items():
            fields = set(schema.get("properties", {}))
            self.assertFalse(fields & FORBIDDEN_PUBLIC_FIELDS, name)
            self.assertFalse(any(field.startswith("channel_") for field in fields), name)
            self.assertFalse(any(field.startswith("local_") for field in fields), name)

        account_fields = set(schemas["ApiAccountOut"]["properties"])
        self.assertTrue({
            "management_account_id",
            "upstream_id",
            "upstream_actual_multiplier",
            "upstream_group_multiplier",
            "upstream_recharge_multiplier",
            "management_billing_multiplier",
            "wallet_balance_usd",
        }.issubset(account_fields))

        history_fields = set(schemas["UpstreamUsageHistoryDayOut"]["properties"])
        self.assertTrue({
            "upstream_wallet_cost_usd",
            "upstream_actual_cost_cny",
            "management_account_cost_usd",
            "management_account_cost_cny",
            "management_user_charge_usd",
            "actual_income_cny",
            "profit_cny",
            "profit_margin",
            "api_accounts",
        }.issubset(history_fields))

    def test_money_models_serialize_explicit_units_only(self) -> None:
        upstream = UpstreamOut(
            upstream_id="00000000-0000-4000-8000-000000000001",
            display_name="Example",
            api_endpoint_url="https://example.test",
            wallet_balance_usd=10,
            actual_balance_cny=1,
            upstream_recharge_multiplier=0.1,
        ).model_dump()
        self.assertEqual(upstream["wallet_balance_usd"], 10)
        self.assertEqual(upstream["actual_balance_cny"], 1)
        self.assertEqual(upstream["upstream_recharge_multiplier"], 0.1)
        self.assertNotIn("balance_remaining", upstream)
        self.assertNotIn("effective_recharge_multiplier", upstream)

        day = UpstreamUsageHistoryDayOut(
            date="2026-08-12",
            upstream_wallet_cost_usd=1,
            upstream_actual_cost_cny=0.1,
            management_account_cost_usd=1.1,
            management_account_cost_cny=0.11,
            management_user_charge_usd=2,
            actual_income_cny=0.2,
        ).model_dump()
        self.assertEqual(day["upstream_wallet_cost_usd"], 1)
        self.assertEqual(day["upstream_actual_cost_cny"], 0.1)
        self.assertEqual(day["management_account_cost_cny"], 0.11)
        self.assertEqual(day["actual_income_cny"], 0.2)
        self.assertNotIn("cost", day)
        self.assertNotIn("sub2api_cost", day)

    def test_all_public_schemas_use_management_site_account_terms(self) -> None:
        schemas = app.openapi()["components"]["schemas"]

        for name, schema in schemas.items():
            fields = set(schema.get("properties", {}))
            self.assertNotIn("sub2api_account_id", fields, name)
            self.assertNotIn("sub2api_imported_at", fields, name)
            self.assertNotIn("sub2api_error_code", fields, name)
            self.assertNotIn("sub2api_error_message", fields, name)

        account_fields = set(schemas["AccountOut"]["properties"])
        self.assertTrue(
            {
                "management_account_id",
                "management_site_imported_at",
                "management_site_error_code",
                "management_site_error_message",
            }.issubset(account_fields)
        )
