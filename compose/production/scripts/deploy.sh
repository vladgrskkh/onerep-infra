#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../compose.yaml"
ENV_FILE="${ONEREP_ENV_FILE:-/opt/onerep/.env}"
PROJECT_NAME="onerep"
NETWORK_NAME="${PROJECT_NAME}_default"
CURL_IMAGE='docker.io/curlimages/curl:8.22.0@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777'
SKIP_PULL=0

usage() {
    printf 'Usage: %s [--env-file PATH]\n' "${0##*/}" >&2
}

while (($#)); do
    case "$1" in
        --env-file)
            if (($# < 2)); then
                usage
                exit 2
            fi
            ENV_FILE="$2"
            shift 2
            ;;
        --skip-pull)
            SKIP_PULL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

if [[ ! -f "$ENV_FILE" ]]; then
    printf 'Environment file is missing\n' >&2
    exit 1
fi
chmod 600 "$ENV_FILE"
if mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null)"; then
    :
else
    mode="$(stat -f '%Lp' "$ENV_FILE")"
fi
if [[ "$mode" != 600 ]]; then
    printf 'Environment file must have mode 0600\n' >&2
    exit 1
fi

compose() {
    docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" "$@"
}

check_http() {
    local url="$1"
    for _attempt in {1..30}; do
        if docker run --rm --network "$NETWORK_NAME" --entrypoint curl "$CURL_IMAGE" \
            --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    printf 'Health check failed for %s\n' "$url" >&2
    return 1
}

if ((SKIP_PULL == 0)); then
    compose pull postgres redis rustfs auth gym-api nginx certbot
fi
compose up --detach --wait postgres redis rustfs
check_http http://rustfs:9000/health/ready
compose --profile bootstrap run --rm rustfs-bootstrap >/dev/null 2>&1

compose run --rm migrate-auth >/dev/null 2>&1
compose run --rm migrate-gym >/dev/null 2>&1
if ! compose --profile deploy run --rm migrate-auth-status >/dev/null 2>&1; then
    printf 'Auth database migration status check failed\n' >&2
    exit 1
fi
if ! compose --profile deploy run --rm migrate-gym-status >/dev/null 2>&1; then
    printf 'Gym database migration status check failed\n' >&2
    exit 1
fi

compose --profile production up --detach --no-deps auth
check_http http://auth:8080/v1/health
check_http http://auth:8080/v1/.well-known/jwks.json
compose --profile production up --detach --no-deps gym-api
check_http http://gym-api:8081/v1/health

certificate_state="$(compose --profile certificate run --rm --no-deps \
    --entrypoint /bin/sh certbot -c \
    'if [ -s /etc/letsencrypt/live/onerep.mooo.com/fullchain.pem ] && [ -s /etc/letsencrypt/live/onerep.mooo.com/privkey.pem ]; then printf CERTIFICATE_STATE_PRESENT; else printf CERTIFICATE_STATE_MISSING; fi')"
case "$certificate_state" in
    *CERTIFICATE_STATE_PRESENT)
        ;;
    *CERTIFICATE_STATE_MISSING)
        compose --profile bootstrap up --detach nginx-bootstrap
        check_http http://nginx-bootstrap/healthz
        "$SCRIPT_DIR/bootstrap-cert.sh" --env-file "$ENV_FILE"
        compose --profile bootstrap stop nginx-bootstrap >/dev/null
        compose --profile bootstrap rm --force nginx-bootstrap >/dev/null
        ;;
    *)
        printf 'Unable to determine certificate state\n' >&2
        exit 1
        ;;
esac

compose --profile production up --detach nginx
check_http https://onerep.mooo.com/auth/v1/health
check_http https://onerep.mooo.com/gym/v1/health
