#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE_FILE=$(cd -- "$SCRIPT_DIR/.." && pwd)/compose.yaml
PROJECT=${COMPOSE_PROJECT_NAME:-onerep}
ENV_FILE=
LOCAL_REPOSITORY=
EXPECTED_MEDIA_SHA256=
EXPECT_AUTH_ROW=
EXPECT_GYM_ROW=
POSTGRES_IMAGE=docker.io/library/postgres:16.15-alpine3.24@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea

fail() {
    printf 'Restore check failed: %s\n' "$1" >&2
    exit 1
}

usage() {
    printf 'Usage: restore-check.sh [--project-name NAME] [--env-file FILE] [--local-repository TEST_NAME] [--expected-media-sha256 HASH] [--expect-auth-row VALUE] [--expect-gym-row VALUE]\n' >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --project-name) (($# >= 2)) || usage; PROJECT=$2; shift 2 ;;
        --env-file) (($# >= 2)) || usage; ENV_FILE=$2; shift 2 ;;
        --local-repository) (($# >= 2)) || usage; LOCAL_REPOSITORY=$2; shift 2 ;;
        --expected-media-sha256) (($# >= 2)) || usage; EXPECTED_MEDIA_SHA256=$2; shift 2 ;;
        --expect-auth-row) (($# >= 2)) || usage; EXPECT_AUTH_ROW=$2; shift 2 ;;
        --expect-gym-row) (($# >= 2)) || usage; EXPECT_GYM_ROW=$2; shift 2 ;;
        *) usage ;;
    esac
done
[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail 'invalid Compose project name'
[[ -z "$ENV_FILE" || -r "$ENV_FILE" ]] || fail 'environment file is not readable'
if [[ -n "$LOCAL_REPOSITORY" ]]; then
    [[ "$PROJECT" =~ ^onerep-backup-check-[0-9a-f]{16}$ && "$LOCAL_REPOSITORY" == "test-$PROJECT" ]] ||
        fail 'local repositories are permitted only in the matching disposable backup test project'
fi

COMPOSE=(docker compose --project-name "$PROJECT" -f "$COMPOSE_FILE")
[[ -z "$ENV_FILE" ]] || COMPOSE+=(--env-file "$ENV_FILE")
RESTORE_ROOT=
TOKEN=
CONTAINER=
CONTAINER_STARTED=0
DB_PASSWORD=

cleanup() {
    local status=$?
    trap - EXIT HUP INT TERM
    if [[ "$CONTAINER_STARTED" -eq 1 ]]; then
        local owner
        owner=$(docker inspect --format '{{ index .Config.Labels "org.onerep.restore-check" }}' "$CONTAINER" 2>/dev/null || true)
        if [[ "$owner" == "$TOKEN" ]]; then
            docker rm --force "$CONTAINER" >/dev/null 2>&1 || status=1
        else
            printf 'Refusing cleanup: restore container ownership label did not match.\n' >&2
            status=1
        fi
    fi
    if [[ "$RESTORE_ROOT" == "${TMPDIR:-/tmp}"/onerep-restore-check.* && -d "$RESTORE_ROOT" && ! -L "$RESTORE_ROOT" ]]; then
        rm -rf -- "$RESTORE_ROOT"
    else
        printf 'Refusing to remove unverified restore staging directory.\n' >&2
        status=1
    fi
    exit "$status"
}

RESTORE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/onerep-restore-check.XXXXXXXX")
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 0700 "$RESTORE_ROOT"
TOKEN=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
[[ "$TOKEN" =~ ^[0-9a-f]{16}$ ]] || fail 'could not create a valid disposable container identity'
CONTAINER="onerep-restore-check-$TOKEN"
DB_PASSWORD=$(openssl rand -hex 32) || fail 'could not generate an isolated database password'

RESTIC=("${COMPOSE[@]}" --profile backup run --rm --no-deps \
    --user "$(id -u):$(id -g)" -e RESTIC_CACHE_DIR=/tmp/restic-cache --volume "$RESTORE_ROOT:/restore")
if [[ -n "$LOCAL_REPOSITORY" ]]; then
    RESTIC+=(-e "RESTIC_REPOSITORY=local:/backup/$LOCAL_REPOSITORY")
fi
RESTIC+=(restic)
"${RESTIC[@]}" restore latest --target /restore \
    >/dev/null 2>&1 || fail 'latest encrypted snapshot could not be restored'
BACKUP_PARENT="$RESTORE_ROOT/backup"
shopt -s nullglob
RESTORED_STAGES=("$BACKUP_PARENT"/onerep-backup-*)
shopt -u nullglob
[[ "${#RESTORED_STAGES[@]}" -eq 1 && -d "${RESTORED_STAGES[0]}" && ! -L "${RESTORED_STAGES[0]}" ]] ||
    fail 'latest snapshot did not restore one uniquely named staging directory'
BACKUP_ROOT=${RESTORED_STAGES[0]}
[[ -s "$BACKUP_ROOT/auth_db.dump" && -s "$BACKUP_ROOT/gym_db.dump" ]] || fail 'snapshot is missing one or both PostgreSQL archives'
[[ -s "$BACKUP_ROOT/media.sha256" && -d "$BACKUP_ROOT/media" ]] || fail 'snapshot is missing its media mirror or checksum manifest'
(cd "$BACKUP_ROOT/media" && sha256sum --check --status "$BACKUP_ROOT/media.sha256") ||
    fail 'restored media checksums do not match the snapshot manifest'
if [[ -n "$EXPECTED_MEDIA_SHA256" ]]; then
    [[ "$EXPECTED_MEDIA_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail 'expected media checksum is malformed'
    actual=$(find "$BACKUP_ROOT/media" -type f -print0 | xargs -0 -r sha256sum | awk 'NR == 1 { print $1 }')
    [[ "$actual" == "$EXPECTED_MEDIA_SHA256" ]] || fail 'restored media checksum differs from the expected fixture'
fi

docker run --detach --name "$CONTAINER" \
    --label "org.onerep.restore-check=$TOKEN" \
    --network none --read-only --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=1g \
    --tmpfs /var/run/postgresql:rw,nosuid,nodev,size=16m \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --env "POSTGRES_PASSWORD=$DB_PASSWORD" "$POSTGRES_IMAGE" >/dev/null ||
    fail 'could not start isolated temporary PostgreSQL'
CONTAINER_STARTED=1
ready=0
for _ in {1..60}; do
    if docker exec "$CONTAINER" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[[ "$ready" -eq 1 ]] || fail 'isolated PostgreSQL did not become ready'

for database in auth_db gym_db; do
    docker exec "$CONTAINER" createdb -U postgres "$database" >/dev/null 2>&1 ||
        fail "could not create isolated $database"
    archive="$BACKUP_ROOT/${database}.dump"
    docker exec -i "$CONTAINER" pg_restore --no-owner --no-privileges \
        -U postgres -d "$database" <"$archive" >/dev/null 2>&1 ||
        fail "could not restore $database into isolated PostgreSQL"
    docker exec "$CONTAINER" psql -U postgres -d "$database" -Atqc \
        'SELECT current_database(), count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('\''pg_catalog'\'', '\''information_schema'\'')' \
        >"$RESTORE_ROOT/${database}.query" 2>/dev/null || fail "could not query restored $database"
done

assert_fixture_row() {
    local database=$1 expected=$2
    [[ -z "$expected" ]] && return 0
    local actual
    actual=$(docker exec "$CONTAINER" psql -U postgres -d "$database" -Atqc \
        'SELECT value FROM backup_probe WHERE id = 1' 2>/dev/null) ||
        fail "could not query the $database fixture row"
    [[ "$actual" == "$expected" ]] || fail "restored $database fixture row does not match"
}
assert_fixture_row auth_db "$EXPECT_AUTH_ROW"
assert_fixture_row gym_db "$EXPECT_GYM_ROW"

printf 'Latest snapshot restored and both isolated databases and media checksums verified.\n'
