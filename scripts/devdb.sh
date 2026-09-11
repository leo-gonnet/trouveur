#!/usr/bin/env bash
#
# Throwaway Postgres for development and for `trouveur eval`.
#
# Two standalone containers, deliberately NOT in compose.yaml. The production stack runs on this
# same host under the `trouveur` compose project, and a `compose down -v` aimed at dev must not be
# able to reach its volume. These carry no volume at all, so `rm -f` is the whole reset story.
#
#   scripts/devdb.sh up                  start both and migrate to head
#   scripts/devdb.sh reset [dev|eval]    destroy and recreate from scratch
#   scripts/devdb.sh down                stop and remove
#   scripts/devdb.sh status
#   scripts/devdb.sh psql [dev|eval]
#   eval "$(scripts/devdb.sh env)"       export DATABASE_URL and the two test/eval URLs
#
set -euo pipefail

IMAGE=pgvector/pgvector:pg16
PASSWORD=dev
DEV_PORT=55432
EVAL_PORT=55433

repo_root() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }
url() { echo "postgresql+asyncpg://trouveur:${PASSWORD}@127.0.0.1:${1}/trouveur"; }

container() { case "$1" in dev) echo trouveur-dev-db;; eval) echo trouveur-eval-db;; *) die "unknown database $1";; esac; }
port()      { case "$1" in dev) echo "$DEV_PORT";; eval) echo "$EVAL_PORT";; *) die "unknown database $1";; esac; }
die()       { echo "$*" >&2; exit 1; }

start_one() {
  local name port
  name=$(container "$1"); port=$(port "$1")
  if [ -n "$(docker ps -q -f "name=^${name}$")" ]; then
    echo "$name already running on :$port"
    return
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    -e POSTGRES_USER=trouveur -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB=trouveur \
    -p "127.0.0.1:${port}:5432" "$IMAGE" >/dev/null
  printf 'waiting for %s' "$name"
  for _ in $(seq 60); do
    if docker exec "$name" pg_isready -U trouveur -d trouveur >/dev/null 2>&1; then
      echo " ready on :$port"; return
    fi
    printf '.'; sleep 1
  done
  die " timed out; see: docker logs $name"
}

migrate_one() {
  ( cd "$(repo_root)" && DATABASE_URL="$(url "$(port "$1")")" uv run alembic upgrade head >/dev/null )
  echo "$1 migrated to head"
}

cmd=${1:-up}
case "$cmd" in
  up)
    for which in dev eval; do start_one "$which"; migrate_one "$which"; done
    echo
    echo 'eval "$(scripts/devdb.sh env)"   # to use them'
    ;;
  reset)
    for which in ${2:-dev eval}; do
      docker rm -f "$(container "$which")" >/dev/null 2>&1 || true
      start_one "$which"; migrate_one "$which"
    done
    ;;
  down)
    for which in dev eval; do docker rm -f "$(container "$which")" >/dev/null 2>&1 || true; done
    echo "removed"
    ;;
  status)
    docker ps -a --filter name=trouveur-dev-db --filter name=trouveur-eval-db \
      --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  psql)
    exec docker exec -it "$(container "${2:-dev}")" psql -U trouveur -d trouveur
    ;;
  env)
    echo "export DATABASE_URL='$(url "$DEV_PORT")'"
    echo "export TROUVEUR_TEST_DATABASE_URL='$(url "$DEV_PORT")'"
    echo "export TROUVEUR_EVAL_DATABASE_URL='$(url "$EVAL_PORT")'"
    ;;
  *) die "usage: devdb.sh {up|reset [dev|eval]|down|status|psql [dev|eval]|env}" ;;
esac
