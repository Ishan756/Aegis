-- Extensions required by the Postgres service in docker-compose.yml.
--
-- Intentionally minimal. This directory is mounted read-only at container init
-- and only runs on an empty data directory.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";