#!/bin/sh
set -eu

fail() {
    printf '%s\n' "RustFS bootstrap: $1" >&2
    exit 1
}

require_value() {
    eval "value=\${$1-}"
    [ -n "$value" ] || fail "required setting $1 is empty"
}

for name in RUSTFS_ACCESS_KEY RUSTFS_SECRET_KEY S3_ACCESS_KEY S3_SECRET_KEY \
    S3_BACKUP_ACCESS_KEY S3_BACKUP_SECRET_KEY S3_BUCKET AWS_DEFAULT_REGION; do
    require_value "$name"
done

RUSTFS_ENDPOINT=${RUSTFS_ENDPOINT:-http://rustfs:9000}
S3_API_POLICY_FILE=${S3_API_POLICY_FILE:-/policies/gym-api-policy.json}
S3_BACKUP_POLICY_FILE=${S3_BACKUP_POLICY_FILE:-/policies/gym-backup-policy.json}

case "$RUSTFS_ENDPOINT" in
    http://*) endpoint_host=${RUSTFS_ENDPOINT#http://} ;;
    *) fail "RUSTFS_ENDPOINT must be an internal http:// endpoint" ;;
esac
case "$endpoint_host" in
    ''|*/*|*@*|*\?*|*\#*) fail "RUSTFS_ENDPOINT must contain only the service host and port" ;;
esac
[ "$S3_ACCESS_KEY" = "gym-api" ] || fail "S3_ACCESS_KEY must be gym-api"
[ "$S3_BACKUP_ACCESS_KEY" = "gym-backup" ] || fail "S3_BACKUP_ACCESS_KEY must be gym-backup"
[ "$S3_BUCKET" = "gym-media" ] || fail "S3_BUCKET must be gym-media"
[ -r "$S3_API_POLICY_FILE" ] || fail "API policy file is unavailable"
[ -r "$S3_BACKUP_POLICY_FILE" ] || fail "backup policy file is unavailable"

umask 077
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM

# rc reads these environment aliases without persisting credentials to disk.
RC_HOST_root="http://${RUSTFS_ACCESS_KEY}:${RUSTFS_SECRET_KEY}@${endpoint_host}"
RC_HOST_api="http://${S3_ACCESS_KEY}:${S3_SECRET_KEY}@${endpoint_host}"
RC_HOST_backup="http://${S3_BACKUP_ACCESS_KEY}:${S3_BACKUP_SECRET_KEY}@${endpoint_host}"
export RC_HOST_root RC_HOST_api RC_HOST_backup

if ! rc bucket create "root/${S3_BUCKET}" --ignore-existing --region "$AWS_DEFAULT_REGION" >/dev/null 2>&1; then
    fail "cannot verify or create bucket ${S3_BUCKET} with the RustFS root identity"
fi

normalize_policy() {
    jq -cS '
        def normalize:
            if type == "object" then with_entries(.value |= normalize)
            elif type == "array" then map(normalize) | sort_by(tojson)
            else . end;
        normalize
    ' "$1"
}

ensure_policy() {
    policy_name=$1
    policy_file=$2
    policy_result=$work_dir/policy.json
    if policy_result=$(rc --json admin policy info root "$policy_name" 2>/dev/null); then
        actual=$(printf '%s' "$policy_result" | jq -cS '(.policy.policy // .policy)' 2>/dev/null) ||
            fail "could not parse existing ${policy_name}; inspect IAM state before retrying"
        expected=$(normalize_policy "$policy_file") || fail "could not parse policy file ${policy_name}"
        actual_normalized=$(printf '%s' "$actual" | jq -cS '
            def normalize:
                if type == "object" then with_entries(.value |= normalize)
                elif type == "array" then map(normalize) | sort_by(tojson)
                else . end;
            normalize
        ' 2>/dev/null) || fail "could not normalize existing ${policy_name}"
        [ "$actual_normalized" = "$expected" ] ||
            fail "policy drift detected for ${policy_name}; review and rotate credentials/policy explicitly"
    else
        result=$?
        [ "$result" -eq 5 ] || fail "cannot inspect ${policy_name}; RustFS returned rc status ${result}"
        rc admin policy create root "$policy_name" "$policy_file" >/dev/null 2>&1 ||
            fail "could not create ${policy_name}"
        policy_result=$(rc --json admin policy info root "$policy_name" 2>/dev/null) ||
            fail "could not verify created ${policy_name}"
        actual=$(printf '%s' "$policy_result" | jq -cS '(.policy.policy // .policy)' 2>/dev/null) ||
            fail "could not parse created ${policy_name}"
        expected=$(normalize_policy "$policy_file") || fail "could not parse policy file ${policy_name}"
        actual_normalized=$(printf '%s' "$actual" | jq -cS '
            def normalize:
                if type == "object" then with_entries(.value |= normalize)
                elif type == "array" then map(normalize) | sort_by(tojson)
                else . end;
            normalize
        ' 2>/dev/null) || fail "could not normalize created ${policy_name}"
        [ "$actual_normalized" = "$expected" ] || fail "created ${policy_name} does not match the checked-in policy"
    fi
}

ensure_user() {
    user_name=$1
    user_secret=$2
    policy_name=$3
    alias_name=$4
    created_user=0
    user_result=$work_dir/user.json
    if user_result=$(rc --json admin user info root "$user_name" 2>/dev/null); then
        :
    else
        result=$?
        [ "$result" -eq 5 ] || fail "cannot inspect IAM user ${user_name}; RustFS returned rc status ${result}"
        # rc JSON contains the supplied secret. Capture both output streams privately.
        add_output=$work_dir/user-add.out
        if ! rc admin user add root "$user_name" "$user_secret" >"$add_output" 2>&1; then
            fail "could not create IAM user ${user_name}"
        fi
        created_user=1
        user_result=$(rc --json admin user info root "$user_name" 2>/dev/null) ||
            fail "could not inspect newly created IAM user ${user_name}"
    fi

    printf '%s' "$user_result" | jq -e --arg username "$user_name" --arg policy "$policy_name" '
        .accessKey == $username and .status == "enabled" and
        ((.memberOf // []) | length == 0) and
        (((.policies // []) | sort) == [] or ((.policies // []) | sort) == [$policy])
    ' >/dev/null 2>&1 ||
        fail "IAM user ${user_name} has credential, status, policy, or group drift; use the documented rotation procedure"

    if [ "$created_user" -eq 0 ]; then
        # Check existing credentials before changing that user's policy attachments.
        self_result=$(rc --json admin user info "$alias_name" "$user_name" 2>/dev/null) ||
            fail "credentials for ${user_name} did not authenticate; rotate the secret explicitly"
        printf '%s' "$self_result" | jq -e --arg username "$user_name" '
            .accessKey == $username and .status == "enabled"
        ' >/dev/null 2>&1 || fail "credential self-verification failed for ${user_name}"
    fi

    policy_list=$(printf '%s' "$user_result" | jq -cS '(.policies // []) | sort') ||
        fail "could not inspect policy attachments for ${user_name}"
    case "$policy_list" in
        '[]')
            rc admin policy attach root "$policy_name" --user "$user_name" >/dev/null 2>&1 ||
                fail "could not attach ${policy_name} to ${user_name}"
            ;;
        "[\"${policy_name}\"]") ;;
        *) fail "IAM user ${user_name} has unexpected policy attachments; rotate or repair explicitly" ;;
    esac

    user_result=$(rc --json admin user info root "$user_name" 2>/dev/null) ||
        fail "could not re-read IAM user ${user_name}"
    printf '%s' "$user_result" | jq -e --arg username "$user_name" --arg policy "$policy_name" '
        .accessKey == $username and .status == "enabled" and
        ((.memberOf // []) | length == 0) and ((.policies // []) == [$policy])
    ' >/dev/null 2>&1 || fail "IAM user ${user_name} did not retain the exact expected policy"

    # Verify supplied credentials after the intended policy is attached (required for a new user).
    self_result=$(rc --json admin user info "$alias_name" "$user_name" 2>/dev/null) ||
        fail "credentials for ${user_name} did not authenticate; rotate the secret explicitly"
    printf '%s' "$self_result" | jq -e --arg username "$user_name" '
        .accessKey == $username and .status == "enabled"
    ' >/dev/null 2>&1 || fail "credential self-verification failed for ${user_name}"
}

ensure_policy gym-api-policy "$S3_API_POLICY_FILE"
ensure_policy gym-backup-policy "$S3_BACKUP_POLICY_FILE"
ensure_user "$S3_ACCESS_KEY" "$S3_SECRET_KEY" gym-api-policy api
ensure_user "$S3_BACKUP_ACCESS_KEY" "$S3_BACKUP_SECRET_KEY" gym-backup-policy backup

# Prove each credential can perform its intended S3 actions, then remove the probe.
probe_source=$work_dir/probe
probe_copy=$work_dir/probe.copy
printf '%s' 'onerep-rustfs-bootstrap-check-v1' >"$probe_source"
probe_key="${S3_BUCKET}/.onerep-bootstrap-check"
rc cp --quiet "$probe_source" "api/${probe_key}" >/dev/null 2>&1 ||
    fail "API credentials failed the allowed PutObject verification"
rc cp --quiet "api/${probe_key}" "$probe_copy" >/dev/null 2>&1 ||
    fail "API credentials failed the allowed GetObject verification"
cmp -s "$probe_source" "$probe_copy" || fail "API object credential verification did not match"
rc cp --quiet "backup/${probe_key}" "$probe_copy" >/dev/null 2>&1 ||
    fail "backup credentials failed the allowed GetObject verification"
cmp -s "$probe_source" "$probe_copy" || fail "backup object credential verification did not match"
rc rm "api/${probe_key}" >/dev/null 2>&1 ||
    fail "API credentials failed the allowed DeleteObject verification"
rm -f "$probe_copy"

printf '%s\n' "RustFS bucket and IAM identities verified: ${S3_BUCKET}, ${S3_ACCESS_KEY}, ${S3_BACKUP_ACCESS_KEY}."
