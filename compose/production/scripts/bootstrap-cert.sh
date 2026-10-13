#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../compose.yaml"
ENV_FILE="${ONEREP_ENV_FILE:-/opt/onerep/.env}"

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
    docker compose --project-name onerep --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" "$@"
}

# This value is non-secret and is read only inside the Certbot container.
# shellcheck disable=SC2016 # This expansion occurs inside the Certbot container.
certbot_email="$(compose --profile certificate run --rm --no-deps \
    --entrypoint /bin/sh certbot -c 'printf "%s" "$CERTBOT_EMAIL"')"
if [[ -z "$certbot_email" ]]; then
    printf 'CERTBOT_EMAIL is required for first certificate issuance\n' >&2
    exit 1
fi

compose --profile certificate run --rm certbot certonly \
    --webroot --webroot-path /var/www/certbot \
    --domain onerep.mooo.com --email "$certbot_email" \
    --agree-tos --non-interactive >/dev/null
