-- Idempotent: create the `trading` postgres role and the `trading` database.
--
-- Run as the postgres OS user (not via TCP/password) so it works on a fresh
-- install where no `trading` role exists yet:
--
--     sudo -u postgres psql -f scripts/setup_trading_role.sql
--
-- The literal 'CHANGE_ME' below is a placeholder so this file is safe to
-- commit. Replace with a real password before running, and use the same
-- value in config.yaml -> postgres.password (or override via env PG_PASSWORD).
-- Never commit a real password.

\echo '== existing roles =='
SELECT rolname FROM pg_roles WHERE rolname IN ('postgres','trading');

\echo '== existing databases =='
SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname = 'trading';

-- Create or update the trading role (idempotent password reset).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading') THEN
        CREATE ROLE trading WITH LOGIN PASSWORD 'CHANGE_ME';
        RAISE NOTICE 'Created role "trading"';
    ELSE
        ALTER ROLE trading WITH LOGIN PASSWORD 'CHANGE_ME';
        RAISE NOTICE 'Updated password on existing role "trading"';
    END IF;
END $$;

-- Create the database if missing (owned by trading).
SELECT 'CREATE DATABASE trading OWNER trading'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'trading')
\gexec

-- Hand ownership over (no-op if already owned by trading) and grant connect.
ALTER DATABASE trading OWNER TO trading;
GRANT ALL PRIVILEGES ON DATABASE trading TO trading;

-- Inside the trading DB: full privileges on public schema, plus default
-- privileges so any future tables created by another superuser also grant.
\connect trading
GRANT ALL ON SCHEMA public TO trading;
GRANT ALL ON ALL TABLES    IN SCHEMA public TO trading;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO trading;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES    TO trading;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO trading;

\echo '== verification =='
SELECT current_database(), current_user;

-- Tables get created on first MCP server / Python startup via
-- db.schema.init_db() — this script intentionally does not bootstrap them.
