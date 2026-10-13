#!/bin/bash
set -Eeuo pipefail

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --set=auth_password="$AUTH_DATABASE_PASSWORD" \
    --set=gym_password="$GYM_DATABASE_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE auth_app LOGIN PASSWORD %L', :'auth_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'auth_app')
\gexec
SELECT format('CREATE ROLE gym_app LOGIN PASSWORD %L', :'gym_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'gym_app')
\gexec
SELECT 'CREATE DATABASE auth_db OWNER auth_app'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'auth_db')
\gexec
SELECT 'CREATE DATABASE gym_db OWNER gym_app'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'gym_db')
\gexec
SQL
