from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models import UpstreamAccountConfig


def test_upstream_identity_columns_compile_as_postgresql_bigint() -> None:
    ddl = str(
        CreateTable(UpstreamAccountConfig.__table__).compile(
            dialect=postgresql.dialect()
        )
    )

    assert "sub2api_account_id BIGINT NOT NULL" in ddl
    assert "upstream_api_key_record_id BIGINT" in ddl
