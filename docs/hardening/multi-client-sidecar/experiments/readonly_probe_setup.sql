\set ON_ERROR_STOP on

CREATE DATABASE sub2api_probe;
\connect sub2api_probe

CREATE SCHEMA sub2api;
CREATE TABLE sub2api.accounts (
    id BIGINT PRIMARY KEY,
    account_type TEXT NOT NULL,
    credentials JSONB NOT NULL,
    internal_note TEXT
);
CREATE TABLE sub2api.admin_secrets (
    id BIGINT PRIMARY KEY,
    secret_value TEXT NOT NULL
);

INSERT INTO sub2api.accounts (id, account_type, credentials, internal_note)
VALUES
    (
        1,
        'api_key',
        '{"api_key":"synthetic-api-key","unrelated_secret":"must-not-be-visible"}',
        'must-not-be-visible'
    ),
    (
        2,
        'oauth',
        '{"access_token":"synthetic-access-token","refresh_token":"synthetic-refresh-token","id_token":"synthetic-id-token"}',
        'must-not-be-visible'
    );
INSERT INTO sub2api.admin_secrets (id, secret_value)
VALUES (1, 'synthetic-admin-secret');

CREATE ROLE gptcheck_connector
    LOGIN
    PASSWORD 'probe-only-password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT;
ALTER ROLE gptcheck_connector SET default_transaction_read_only = on;
ALTER ROLE gptcheck_connector SET statement_timeout = '3s';
ALTER ROLE gptcheck_connector SET idle_in_transaction_session_timeout = '5s';

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA sub2api FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA sub2api FROM PUBLIC;

CREATE SCHEMA connector;
REVOKE ALL ON SCHEMA connector FROM PUBLIC;

CREATE VIEW connector.api_key_inspection_inputs
WITH (security_barrier = true)
AS
SELECT
    id AS account_id,
    credentials ->> 'api_key' AS api_key
FROM sub2api.accounts
WHERE account_type = 'api_key';

CREATE VIEW connector.oauth_inspection_inputs
WITH (security_barrier = true)
AS
SELECT
    id AS account_id,
    credentials ->> 'access_token' AS access_token,
    NULLIF(credentials ->> 'refresh_token', '') IS NOT NULL AS refresh_token_present
FROM sub2api.accounts
WHERE account_type = 'oauth';

GRANT CONNECT ON DATABASE sub2api_probe TO gptcheck_connector;
GRANT USAGE ON SCHEMA connector TO gptcheck_connector;
GRANT SELECT ON connector.api_key_inspection_inputs TO gptcheck_connector;
GRANT SELECT ON connector.oauth_inspection_inputs TO gptcheck_connector;
