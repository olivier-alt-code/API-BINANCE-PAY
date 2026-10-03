#!/bin/sh
# Optionally apply migrations before starting (used on Azure App Service, where there is
# no separate "migrate" container). Keep a single instance when enabling it, so two
# replicas never migrate at the same time.
set -e
if [ "${RUN_MIGRATIONS_ON_START:-false}" = "true" ]; then
    alembic upgrade head
fi
exec "$@"
