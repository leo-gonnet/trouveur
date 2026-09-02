#!/usr/bin/env bash
# Nightly logical backup with retention. Run from the compose project directory on the host.
# Dumps out of the db container, so it needs no host Postgres client and no password handling.
set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-$HOME/trouveur-backups}
RETENTION_DAYS=${RETENTION_DAYS:-14}

mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${BACKUP_DIR}/trouveur-${STAMP}.dump"

docker compose exec -T db \
    pg_dump --format=custom --no-owner --username=trouveur trouveur > "${OUT}"
echo "wrote ${OUT} ($(du -h "${OUT}" | cut -f1))"

find "${BACKUP_DIR}" -name 'trouveur-*.dump' -type f -mtime "+${RETENTION_DAYS}" -delete
echo "pruned dumps older than ${RETENTION_DAYS} days"
