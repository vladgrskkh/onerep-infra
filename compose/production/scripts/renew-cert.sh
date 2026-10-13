#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../compose.yaml"
ENV_FILE="${ONEREP_ENV_FILE:-/opt/onerep/.env}"
PROJECT_NAME="onerep"

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

certificate_hash() {
    local output
    output="$(compose --profile certificate run --rm --no-deps \
        --entrypoint /bin/sh certbot -c \
        'sha256sum /etc/letsencrypt/live/onerep.mooo.com/fullchain.pem')"
    local hash="${output%% *}"
    if [[ ! "$hash" =~ ^[[:xdigit:]]{64}$ ]]; then
        printf 'Unable to read the active certificate fingerprint\n' >&2
        return 1
    fi
    printf '%s' "$hash"
}

before="$(certificate_hash)"
compose --profile certificate run --rm certbot renew \
    --webroot --webroot-path /var/www/certbot --non-interactive >/dev/null
after="$(certificate_hash)"

if [[ "$before" != "$after" ]]; then
    nginx_container="$(compose --profile production ps --status running --quiet nginx)"
    if [[ -n "$nginx_container" ]]; then
        compose --profile production exec --no-TTY nginx nginx -s reload >/dev/null
    fi
fi
