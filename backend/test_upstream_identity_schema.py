from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models import ApiAccount


def test_upstream_identity_columns_compile_as_postgresql_bigint() -> None:
    ddl = str(
        CreateTable(ApiAccount.__table__).compile(
            dialect=postgresql.dialect()
        )
    )

    assert "management_account_id BIGINT NOT NULL" in ddl
    assert "remote_upstream_api_key_id BIGINT" in ddl
