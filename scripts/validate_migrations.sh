#!/usr/bin/env bash
# Apply the Stride migrations in timestamp order against a disposable,
# Supabase-compatible PostgreSQL database. Fails on the first error.
#
# Usage:
#   DATABASE_URL=postgres://postgres:postgres@127.0.0.1:5432/postgres \
#     scripts/validate_migrations.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
migrations_dir="${MIGRATIONS_DIR:-$repo_root/supabase/migrations}"
bootstrap="${BOOTSTRAP_SQL:-$repo_root/supabase/tests/bootstrap.sql}"
database_url="${DATABASE_URL:?DATABASE_URL is required}"

psql_base=(psql "$database_url" -v ON_ERROR_STOP=1 -q)

echo "Applying bootstrap: $bootstrap"
"${psql_base[@]}" -f "$bootstrap"

for migration in $(LC_ALL=C ls "$migrations_dir"/*.sql | sort); do
  echo "Applying migration: $(basename "$migration")"
  "${psql_base[@]}" -f "$migration"
done

echo "All migrations applied successfully."
