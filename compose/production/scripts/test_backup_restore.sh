#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PRODUCTION_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ROOT=$(cd -- "$PRODUCTION_DIR/../.." && pwd)
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"
RESTORE_SCRIPT="$SCRIPT_DIR/restore-check.sh"
TEST_USER="$(id -u):$(id -g)"

fail() {
    printf 'Backup/restore smoke test: %s\n' "$1" >&2
    exit 1
}

[[ -x "$BACKUP_SCRIPT" ]] || fail 'backup.sh is missing or not executable'
[[ -x "$RESTORE_SCRIPT" ]] || fail 'restore-check.sh is missing or not executable'
command -v docker >/dev/null || fail 'Docker is required for this disposable integration test'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose is required for this disposable integration test'

PROJECT="onerep-backup-check-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$PROJECT" =~ ^onerep-backup-check-[0-9a-f]{16}$ ]] || fail 'generated an invalid project name'
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/${PROJECT}.XXXXXX")
chmod 700 "$TEMP_ROOT"
ENV_FILE="$TEMP_ROOT/test.env"
LOCAL_REPOSITORY="test-$PROJECT"
COMPOSE=(docker compose --project-name "$PROJECT" --env-file "$ENV_FILE" -f "$PRODUCTION_DIR/compose.yaml")
CLEANUP_COMPOSE=("${COMPOSE[@]}" --profile '*')

cleanup() {
    local status=$?
    local safe_to_teardown=1
    trap - EXIT INT TERM HUP
    if [[ "$PROJECT" =~ ^onerep-backup-check-[0-9a-f]{16}$ ]]; then
        local container_ids container_id owner volume_ids volume_id network_ids network_id
        container_ids=$("${CLEANUP_COMPOSE[@]}" ps --all --quiet 2>/dev/null) || safe_to_teardown=0
        for container_id in $container_ids; do
            owner=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id" 2>/dev/null || true)
            [[ "$owner" == "$PROJECT" ]] || {
                printf 'Refusing cleanup: container %s is not labeled for %s\n' "$container_id" "$PROJECT" >&2
                safe_to_teardown=0
                status=1
            }
        done
        volume_ids=$(docker volume ls --quiet --filter "label=com.docker.compose.project=$PROJECT") || safe_to_teardown=0
        for volume_id in $volume_ids; do
            owner=$(docker volume inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$volume_id" 2>/dev/null || true)
            [[ "$owner" == "$PROJECT" ]] || {
                printf 'Refusing cleanup: volume %s is not labeled for %s\n' "$volume_id" "$PROJECT" >&2
                safe_to_teardown=0
                status=1
            }
        done
        network_ids=$(docker network ls --quiet --filter "label=com.docker.compose.project=$PROJECT") || safe_to_teardown=0
        for network_id in $network_ids; do
            owner=$(docker network inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$network_id" 2>/dev/null || true)
            [[ "$owner" == "$PROJECT" ]] || {
                printf 'Refusing cleanup: network %s is not labeled for %s\n' "$network_id" "$PROJECT" >&2
                safe_to_teardown=0
                status=1
            }
        done
        if [[ "$safe_to_teardown" -eq 1 ]]; then
            "${CLEANUP_COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || {
                printf 'Disposable Compose teardown failed for %s\n' "$PROJECT" >&2
                status=1
            }
        else
            printf 'Compose cleanup skipped because project resource ownership could not be verified.\n' >&2
            status=1
        fi
    else
        printf 'Refusing cleanup for unverified project name: %s\n' "$PROJECT" >&2
        status=1
    fi
    rm -rf -- "$TEMP_ROOT"
    return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# This is the checked-in dummy-only example, never the server's .env.
cp "$ROOT/.env.example" "$ENV_FILE"
chmod 600 "$ENV_FILE"
sed -i.bak 's|^RESTIC_REPOSITORY=.*$|RESTIC_REPOSITORY=/repository|' "$ENV_FILE"
rm -f "$ENV_FILE.bak"
cat >>"$ENV_FILE" <<'EOF'
AWS_ACCESS_KEY_ID=test-offsite-access-key
AWS_SECRET_ACCESS_KEY=test-offsite-secret-key-only
EOF

"${COMPOSE[@]}" up --detach --wait --wait-timeout 120 postgres rustfs
"${COMPOSE[@]}" --profile bootstrap run --rm --no-deps rustfs-bootstrap >/dev/null
# shellcheck disable=SC2016
"${COMPOSE[@]}" exec -T postgres sh -ec 'PGPASSWORD="$AUTH_DATABASE_PASSWORD" psql -U auth_app -d auth_db -v ON_ERROR_STOP=1 -c "CREATE TABLE IF NOT EXISTS backup_probe (id integer PRIMARY KEY, value text NOT NULL)" -c "INSERT INTO backup_probe VALUES (1, '\''auth-fixture'\'') ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value"' >/dev/null
# shellcheck disable=SC2016
"${COMPOSE[@]}" exec -T postgres sh -ec 'PGPASSWORD="$GYM_DATABASE_PASSWORD" psql -U gym_app -d gym_db -v ON_ERROR_STOP=1 -c "CREATE TABLE IF NOT EXISTS backup_probe (id integer PRIMARY KEY, value text NOT NULL)" -c "INSERT INTO backup_probe VALUES (1, '\''gym-fixture'\'') ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value"' >/dev/null

mkdir -m 700 "$TEMP_ROOT/seed-media"
PAYLOAD="$TEMP_ROOT/seed-media/media-fixture.bin"
printf 'one-rep-disposable-media-fixture\n' >"$PAYLOAD"
EXPECTED_MEDIA_SHA256=$(shasum -a 256 "$PAYLOAD" | awk '{print $1}')
ROOT_ALIAS='http://AAAAAAAAAAAAAAAAAAAA:DUMMY-rustfs-root-secret-00000000000000000000000000000000@rustfs:9000'
"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" --entrypoint rc \
    --volume "$TEMP_ROOT:/test:ro" -e "RC_HOST_root=$ROOT_ALIAS" rustfs-backup \
    --json --no-progress mirror /test/seed-media/ root/gym-media/ --summary >/dev/null 2>&1 ||
    fail 'could not seed the disposable media bucket'

"$BACKUP_SCRIPT" --project-name "$PROJECT" --env-file "$ENV_FILE" \
    --local-repository "$LOCAL_REPOSITORY"
STAGING_LEFTOVER=$("${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" \
    backup-tools -ec "find /backup -mindepth 1 -maxdepth 1 -type d -name 'onerep-backup-*' -print -quit")
[[ -z "$STAGING_LEFTOVER" ]] || fail 'plaintext staging directory remained after backup completion'
"$RESTORE_SCRIPT" --project-name "$PROJECT" --env-file "$ENV_FILE" \
    --local-repository "$LOCAL_REPOSITORY" --expected-media-sha256 "$EXPECTED_MEDIA_SHA256" \
    --expect-auth-row auth-fixture --expect-gym-row gym-fixture

"${COMPOSE[@]}" --profile backup run --rm --no-deps \
    --user "$TEST_USER" -e RESTIC_CACHE_DIR=/tmp/restic-cache \
    --volume "$TEMP_ROOT:/test" -e "RESTIC_REPOSITORY=local:/backup/$LOCAL_REPOSITORY" \
    restic restore latest --target /test/recovered \
    >/dev/null 2>&1 || fail 'could not restore media for the distinct-bucket test'

RESTORE_BUCKET="restore-check-${PROJECT#onerep-backup-check-}"
WRITER_USER="restore-check-$PROJECT"
WRITER_POLICY="restore-check-$PROJECT"
WRITER_SECRET="temporary-writer-${PROJECT#onerep-backup-check-}"
cat >"$TEMP_ROOT/writer-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::$RESTORE_BUCKET"]},{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":["arn:aws:s3:::$RESTORE_BUCKET/*"]}]}
EOF
chmod 0600 "$TEMP_ROOT/writer-policy.json"
"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" --entrypoint rc \
    --volume "$TEMP_ROOT:/test:ro" -e "RC_HOST_root=$ROOT_ALIAS" rustfs-backup \
    bucket create "root/$RESTORE_BUCKET" --ignore-existing --region us-east-1 >/dev/null 2>&1 ||
    fail 'could not create the distinct disposable restore bucket'
"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" --entrypoint rc \
    --volume "$TEMP_ROOT:/test:ro" -e "RC_HOST_root=$ROOT_ALIAS" rustfs-backup \
    admin policy create root "$WRITER_POLICY" /test/writer-policy.json >/dev/null 2>&1 ||
    fail 'could not create the temporary restore-writer policy'
"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" --entrypoint rc \
    -e "RC_HOST_root=$ROOT_ALIAS" rustfs-backup \
    admin user add root "$WRITER_USER" "$WRITER_SECRET" >/dev/null 2>&1 ||
    fail 'could not create the temporary restore-writer identity'
"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" --entrypoint rc \
    -e "RC_HOST_root=$ROOT_ALIAS" rustfs-backup \
    admin policy attach root "$WRITER_POLICY" --user "$WRITER_USER" >/dev/null 2>&1 ||
    fail 'could not scope the temporary writer to its disposable bucket'
WRITER_ALIAS="http://$WRITER_USER:$WRITER_SECRET@rustfs:9000"
RESTORED_STAGE_NAME=$(find "$TEMP_ROOT/recovered/backup" -mindepth 1 -maxdepth 1 -type d -name 'onerep-backup-*' -print -quit)
[[ "$(basename "$RESTORED_STAGE_NAME")" =~ ^onerep-backup-[0-9a-f]{16}$ ]] ||
    fail 'could not identify the restored staging directory'
RESTORED_STAGE="$TEMP_ROOT/recovered/backup/$(basename "$RESTORED_STAGE_NAME")"
[[ -d "$RESTORED_STAGE/media" ]] || fail 'restored snapshot staging directory is missing'
"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" --entrypoint rc \
    --volume "$TEMP_ROOT:/test:ro" -e "RC_HOST_writer=$WRITER_ALIAS" rustfs-backup \
    --json --no-progress mirror "/test/recovered/backup/$(basename "$RESTORED_STAGE")/media/" "writer/$RESTORE_BUCKET/" --summary \
    >/dev/null 2>&1 || fail 'could not mirror verified media into the distinct disposable bucket'
"${COMPOSE[@]}" --profile backup run --rm --no-deps --user "$TEST_USER" --entrypoint rc \
    --volume "$TEMP_ROOT:/test" -e "RC_HOST_writer=$WRITER_ALIAS" rustfs-backup \
    --json --no-progress mirror "writer/$RESTORE_BUCKET/" /test/recovered-media/ --summary \
    >/dev/null 2>&1 || fail 'could not verify the distinct restore bucket contents'
RESTORED_MEDIA_SHA256=$(shasum -a 256 "$TEMP_ROOT/recovered-media/media-fixture.bin" | awk '{print $1}')
[[ "$RESTORED_MEDIA_SHA256" == "$EXPECTED_MEDIA_SHA256" ]] || fail 'distinct-bucket media checksum differs'

printf 'Backup/restore smoke test passed for disposable project %s\n' "$PROJECT"
