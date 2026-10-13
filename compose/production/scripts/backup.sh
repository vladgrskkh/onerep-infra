#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPOSE_FILE=$(cd -- "$SCRIPT_DIR/.." && pwd)/compose.yaml
PROJECT=${COMPOSE_PROJECT_NAME:-onerep}
ENV_FILE=
LOCAL_REPOSITORY=
RUN_USER="$(id -u):$(id -g)"

fail() {
    printf 'Backup failed: %s\n' "$1" >&2
    exit 1
}

usage() {
    printf 'Usage: backup.sh [--project-name NAME] [--env-file FILE] [--local-repository TEST_NAME]\n' >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --project-name) (($# >= 2)) || usage; PROJECT=$2; shift 2 ;;
        --env-file) (($# >= 2)) || usage; ENV_FILE=$2; shift 2 ;;
        --local-repository) (($# >= 2)) || usage; LOCAL_REPOSITORY=$2; shift 2 ;;
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
BACKUP_TOOLS=("${COMPOSE[@]}" --profile backup run --rm --no-deps -T --user "$RUN_USER" backup-tools)
volume_shell() {
    "${BACKUP_TOOLS[@]}" -ec "$1"
}
volume_root_shell() {
    "${COMPOSE[@]}" --profile backup run --rm --no-deps backup-tools -ec "$1"
}
TEMP_FILES=()

TOKEN=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
[[ "$TOKEN" =~ ^[0-9a-f]{16}$ ]] || fail 'could not generate an isolated staging identifier'
STAGING_NAME="onerep-backup-$TOKEN"
STAGING_PATH="/backup/$STAGING_NAME"
STAGING_CREATED=0
if [[ -n "$LOCAL_REPOSITORY" ]]; then
    REPOSITORY_PATH="/backup/$LOCAL_REPOSITORY"
    volume_root_shell "mkdir -m 0700 '$REPOSITORY_PATH' && chown '$RUN_USER' '$REPOSITORY_PATH'" ||
        fail 'could not create the disposable local Restic repository directory'
fi

cleanup() {
    local status=$?
    trap - EXIT HUP INT TERM
    if [[ "$STAGING_NAME" =~ ^onerep-backup-[0-9a-f]{16}$ && "$STAGING_CREATED" -eq 1 ]]; then
        volume_root_shell "rm -rf -- '$STAGING_PATH'" >/dev/null 2>&1 || {
            printf "Could not remove this run's private staging directory from the backup volume.\n" >&2
            status=1
        }
    fi
    local temporary_path
    for temporary_path in "${TEMP_FILES[@]}"; do
        rm -f -- "$temporary_path"
    done
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

AUTH_DUMP_SIZE="${TMPDIR:-/tmp}/onerep-auth-dump-size-$TOKEN"
GYM_DUMP_SIZE="${TMPDIR:-/tmp}/onerep-gym-dump-size-$TOKEN"
MEDIA_SIZE="${TMPDIR:-/tmp}/onerep-media-size-$TOKEN"
AVAILABLE_SIZE="${TMPDIR:-/tmp}/onerep-staging-free-$TOKEN"
TEMP_FILES+=("$AUTH_DUMP_SIZE" "$GYM_DUMP_SIZE" "$MEDIA_SIZE" "$AVAILABLE_SIZE")
set -o pipefail
"${COMPOSE[@]}" exec -T postgres sh -ec 'exec pg_dump -Fc -U auth_app -d auth_db' 2>/dev/null |
    wc -c >"$AUTH_DUMP_SIZE" || fail 'could not measure the auth_db custom-format dump'
"${COMPOSE[@]}" exec -T postgres sh -ec 'exec pg_dump -Fc -U gym_app -d gym_db' 2>/dev/null |
    wc -c >"$GYM_DUMP_SIZE" || fail 'could not measure the gym_db custom-format dump'

"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$RUN_USER" rustfs-backup \
    --json --no-progress du backup/gym-media --fallback \
    >"$MEDIA_SIZE" 2>/dev/null || fail 'could not measure the RustFS media bucket'
# shellcheck disable=SC2016
volume_shell 'df -Pk /backup | awk '\''NR == 2 { print $4 }'\''' >"$AVAILABLE_SIZE" ||
    fail 'could not measure free space on the Compose staging volume'

python3 - "$AUTH_DUMP_SIZE" "$GYM_DUMP_SIZE" "$MEDIA_SIZE" "$AVAILABLE_SIZE" <<'PY'
import json
import math
import sys

auth_dump_file, gym_dump_file, media_file, available_file = sys.argv[1:]
with open(auth_dump_file, encoding="ascii") as source:
    auth_dump_bytes = int(source.read().strip())
with open(gym_dump_file, encoding="ascii") as source:
    gym_dump_bytes = int(source.read().strip())
if min(auth_dump_bytes, gym_dump_bytes) < 0:
    raise SystemExit("invalid database dump size response")
with open(media_file, encoding="utf-8") as source:
    report = json.load(source)
media_bytes = report.get("data", {}).get("total_bytes")
if not isinstance(media_bytes, int) or media_bytes < 0:
    raise SystemExit("invalid media size response")
if report.get("partial") is True or report.get("data", {}).get("partial") is True:
    raise SystemExit("RustFS returned a partial media size report")
with open(available_file, encoding="utf-8") as source:
    available_kib = int(source.read().strip())
required_bytes = math.ceil((auth_dump_bytes + gym_dump_bytes + media_bytes) * 1.20)
available_bytes = available_kib * 1024
if available_bytes < required_bytes:
    raise SystemExit(
        f"insufficient staging-volume space: need {required_bytes} bytes including 20% headroom, have {available_bytes}"
    )
PY
rm -f -- "${TEMP_FILES[@]}"
TEMP_FILES=()

volume_root_shell "mkdir -m 0700 '$STAGING_PATH'" || fail 'could not create private staging directory on the backup volume'
STAGING_CREATED=1
volume_root_shell "chown '$RUN_USER' '$STAGING_PATH' && mkdir -m 0700 '$STAGING_PATH/media' && chown '$RUN_USER' '$STAGING_PATH/media'" ||
    fail 'could not prepare private staging directory permissions'

if ! {
    "${COMPOSE[@]}" exec -T postgres sh -ec 'exec pg_dump -Fc -U auth_app -d auth_db' 2>/dev/null |
        "${BACKUP_TOOLS[@]}" -ec "umask 077; cat > '$STAGING_PATH/auth_db.dump'"
}; then
    fail 'auth_db custom-format dump failed'
fi
if ! {
    "${COMPOSE[@]}" exec -T postgres sh -ec 'exec pg_dump -Fc -U gym_app -d gym_db' 2>/dev/null |
        "${BACKUP_TOOLS[@]}" -ec "umask 077; cat > '$STAGING_PATH/gym_db.dump'"
}; then
    fail 'gym_db custom-format dump failed'
fi

"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$RUN_USER" rustfs-backup \
    --json --no-progress mirror backup/gym-media/ "$STAGING_PATH/media/" --summary \
    >/dev/null 2>&1 || fail 'RustFS media mirror failed'
volume_shell "umask 077; (cd '$STAGING_PATH/media' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) > '$STAGING_PATH/media.sha256'" ||
    fail 'could not create the media checksum manifest'

RESTIC=("${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$RUN_USER" \
    -e RESTIC_CACHE_DIR=/tmp/restic-cache)
if [[ -n "$LOCAL_REPOSITORY" ]]; then
    RESTIC+=(-e "RESTIC_REPOSITORY=local:$REPOSITORY_PATH")
fi
RESTIC+=(restic)
restic() {
    "${RESTIC[@]}" "$@"
}
restic cat config >/dev/null 2>&1 || restic init >/dev/null 2>&1 || fail 'Restic repository initialization failed'
restic backup --host onerep --tag onerep-production "/backup/$STAGING_NAME" \
    >/dev/null 2>&1 || fail 'encrypted Restic snapshot creation failed'

WEEK=$(date -u +%G-%V)
LAST_WEEK=$("${COMPOSE[@]}" --profile backup run --rm --no-deps backup-tools -ec 'cat /state/last-prune-week 2>/dev/null || true')
if [[ "$LAST_WEEK" != "$WEEK" ]]; then
    restic forget --tag onerep-production --keep-daily 7 --keep-weekly 5 \
        --keep-monthly 6 --prune >/dev/null 2>&1 || fail 'weekly Restic retention/prune failed'
    "${COMPOSE[@]}" --profile backup run --rm --no-deps backup-tools -ec "printf '%s\\n' '$WEEK' > /state/last-prune-week.tmp && chmod 0600 /state/last-prune-week.tmp && mv -f /state/last-prune-week.tmp /state/last-prune-week" ||
        fail 'could not record the weekly prune date'
fi

printf 'Encrypted backup snapshot created.\n'
