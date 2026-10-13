# OneRep infrastructure

This public repository contains the Compose deployment and operating runbook for the OneRep pilot. It uses one PostgreSQL server with separate `auth_db`/`auth_app` and `gym_db`/`gym_app` identities, private Redis and RustFS services, and NGINX at `onerep.mooo.com`. NGINX serves the mobile API bases `https://onerep.mooo.com/auth` and `https://onerep.mooo.com/gym`, and preserves the signed Host, path, and query on `/gym-media/` requests.

Configuration examples contain dummy values only. Put real values in the protected GitHub `production` Environment and in an untracked server `.env` file. Deployment is a deliberate operator action; application merges do not deploy automatically.

## Provision the server

Use a supported Linux host with Docker Engine and the Compose plugin. Point `onerep.mooo.com` at the server, and allow inbound SSH, HTTP, and HTTPS only. Keep PostgreSQL, Redis, application ports, and the RustFS API and console unpublished.

Check out a reviewed infrastructure commit, then create the server environment file:

```sh
cp .env.example .env
chmod 600 .env
```

Replace both application image tags with the immutable short Git commit tags published by the backend repositories. Generate each database password, the Redis password, the RustFS root secret, both S3 user secrets, the Restic repository password, and the offsite S3 secret with `openssl rand -hex 32`. Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` to the credentials issued by the offsite storage provider. Generate the auth service's RSA signing key with `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out auth-jwt-private.pem`; place its PEM contents in `AUTH_JWT_PRIVATE_KEY` as a quoted multiline value in `.env`. Generate the RustFS root access key with:

```sh
openssl rand -hex 10 | tr '[:lower:]' '[:upper:]'
```

That produces the required 20 uppercase hexadecimal characters. Keep the fixed S3 access keys as `gym-api` and `gym-backup`. Generate the auth signing key in the format expected by the auth service, and set a real certificate contact email. Never put generated values in shell commands, CI logs, source control, or this public repository. Do not pass the entire `.env` file to containers: Compose maps only each service's required values.

Validate the file and Compose model before starting services:

```sh
docker compose --env-file .env -f compose/production/compose.yaml config --quiet
```

## Manually deploy from GitHub

The `.github/workflows/deploy-production.yml` workflow runs only through **Actions → deploy production → Run workflow**. It asks for immutable auth and gym API commit tags and uses the protected GitHub Environment named `production`; configure required reviewers there before enabling production credentials. Application pushes and pull requests never deploy.

Add these GitHub Environment secrets:

- `POSTGRES_ADMIN_PASSWORD`, `AUTH_DATABASE_PASSWORD`, `GYM_DATABASE_PASSWORD`, and `REDIS_PASSWORD`
- `AUTH_JWT_PRIVATE_KEY`
- `RUSTFS_ACCESS_KEY` and `RUSTFS_SECRET_KEY`
- `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BACKUP_ACCESS_KEY`, and `S3_BACKUP_SECRET_KEY`
- `RESTIC_REPOSITORY` and `RESTIC_PASSWORD`
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for the offsite Restic S3 provider
- `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_PRIVATE_KEY`, and `DEPLOY_KNOWN_HOSTS`
- `GHCR_PULL_TOKEN`, a read-only GitHub Container Registry token for the private application images

Add `CERTBOT_EMAIL` and `AWS_DEFAULT_REGION` as non-secret Environment variables. Set the region to `us-east-1`. The fixed S3 bucket and internal/public service endpoints are supplied by the workflow.

Generate one stable RSA key for auth and store its complete PEM in `AUTH_JWT_PRIVATE_KEY`:

```sh
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt-private.pem
```

Do not regenerate the key for routine deployments; existing tokens and clients depend on it. For an S3-compatible offsite backup provider, put its endpoint directly in `RESTIC_REPOSITORY`, for example `s3:https://endpoint.example/bucket/path`. Do not rely on an `AWS_ENDPOINT_URL` override.

The workflow renders a private `.env` with mode `0600`, transfers the tracked Compose bundle and environment over SSH, sends the GHCR token through SSH standard input, and removes its temporary server-side Docker credential directory as soon as image pulling finishes. The deployment script then performs migrations, health checks, first-certificate bootstrap when needed, and starts TLS NGINX.

## First deployment

Start the private data services and wait for their health checks:

```sh
docker compose --env-file .env -f compose/production/compose.yaml up -d postgres redis rustfs
docker compose --env-file .env -f compose/production/compose.yaml ps
```

Run the idempotent RustFS bucket and IAM bootstrap. It creates `gym-media`, the exact checked-in API and read-only backup policies, and the fixed IAM users. It verifies each credential with signed identity lookup and allowed object operations. If a password or policy has drifted, it stops and preserves the existing state; rotate deliberately, update `.env`, then rerun.

```sh
docker compose --profile bootstrap --env-file .env -f compose/production/compose.yaml run --rm rustfs-bootstrap
```

Initialize the public HTTP challenge endpoint, then request the first certificate:

```sh
docker compose --profile bootstrap --env-file .env -f compose/production/compose.yaml up -d nginx-bootstrap
docker compose --profile certificate --env-file .env -f compose/production/compose.yaml run --rm certbot \
  certonly --webroot --webroot-path /var/www/certbot \
  -d onerep.mooo.com --email operator@example.com --agree-tos --non-interactive
docker compose --profile bootstrap --env-file .env -f compose/production/compose.yaml stop nginx-bootstrap
```

Replace the example email with the same real value set in `.env`. Run each service's migrations independently, then start the API services and TLS NGINX:

```sh
docker compose --env-file .env -f compose/production/compose.yaml run --rm migrate-auth
docker compose --env-file .env -f compose/production/compose.yaml run --rm migrate-gym
docker compose --profile production --env-file .env -f compose/production/compose.yaml up -d auth gym-api nginx
```

The database initialization script runs only when PostgreSQL starts with a new data volume. On an existing volume it does not create or rotate roles. To rotate a database password, connect as the PostgreSQL administrator, use `\password auth_app` or `\password gym_app` interactively, update the matching `.env` value, and recreate the matching migration and application containers. Do not put a password in the `psql` command line.

## Health, logs, and capacity

Check service health and recent logs after startup:

```sh
docker compose --env-file .env -f compose/production/compose.yaml ps
docker compose --env-file .env -f compose/production/compose.yaml logs --tail=100 postgres redis rustfs auth gym-api nginx
docker stats
df -h
docker system df
```

Every long-running service has a health check, restart policy, and bounded `json-file` logs (10 MB per file, three files). RustFS uses stdout-only application logging with an empty log directory and empty OTLP endpoints; its stdout and stderr are captured by Docker's rotating log driver. Watch both Docker storage and the host filesystem, especially PostgreSQL, RustFS, and backup staging. Avoid automatic prune commands: they can remove image layers needed for rollback.

Install and enable the included daily renewal timer once after the first deployment:

```sh
sudo install -m 0644 compose/production/systemd/onerep-cert-renew.service /etc/systemd/system/
sudo install -m 0644 compose/production/systemd/onerep-cert-renew.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now onerep-cert-renew.timer
systemctl list-timers onerep-cert-renew.timer
```

The renewal script compares the active certificate fingerprint before and after Certbot runs and reloads production NGINX only when the certificate changes.

## Image updates and rollback

Use immutable application commit tags and the exact upstream tags and registry digests recorded in `compose/production/compose.yaml`. Review an upstream release and its digest before changing either. Update `.env`, validate the rendered Compose model, pull the selected images, and recreate only the affected services. Keep the previous immutable tags recorded so rollback is a tag change followed by recreation. Database migrations may not be reversible; review the backend migration history before rolling an application image back.

## Credential rotation

Keep PostgreSQL administrator credentials confined to PostgreSQL and one-shot administration. App and migration containers receive only their database role credentials. Redis, RustFS root, API S3, backup S3, and Restic credentials are likewise limited to their consumers. The RustFS root identity is available only to the server and one-shot bootstrap/admin containers; the API and Restic backup service never receive it.

RustFS bootstrap intentionally rejects a changed secret for an existing IAM user because the secret cannot be read back for comparison. Use a reviewed RustFS administrative credential-rotation procedure to update the existing `gym-api` or `gym-backup` user in place while preserving its exact policy and fixed access key. Update the matching protected production secret and server `.env` value together, recreate the consuming service, then rerun bootstrap to verify the new credential. Do not delete and recreate a user as an automatic repair. If the stored policy differs, review the access change explicitly before applying the checked-in policy.

Rotate Redis credentials as a coordinated change: update `.env`, recreate Redis and both applications, then confirm all health checks. For JWT signing key rotation, account for existing tokens and client key discovery before replacing the stable key. Restrict and audit access to the server `.env`; remove retired secrets from the active secret store only after consumers are confirmed healthy.

## Backup and restore

The `backup` profile contains one-shot Restic, pinned `rustfs/rc`, and filesystem helper clients. Daily backups read `auth_db`, `gym_db`, and the current objects in `gym-media` using the read-only `gym-backup` identity. The host script first measures database and media sizes and checks the Compose staging volume for at least 20% headroom, then writes `pg_dump -Fc` archives, mirrors media into a fresh directory, and records a SHA-256 manifest. The temporary mode-0700 staging directory lives inside the `backup-staging` Docker volume and is removed on every exit path. Restic reads that volume directly and submits the files to the encrypted repository identified by `RESTIC_REPOSITORY`; keep that repository off the application server. `RESTIC_PASSWORD` encrypts the snapshots, while `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` authenticate to the offsite S3 provider. Logs contain operation status only, never credentials.

Install and enable the daily timer during server provisioning after `/opt/onerep` and Docker are configured:

```sh
sudo install -m 0644 compose/production/systemd/onerep-backup.service /etc/systemd/system/
sudo install -m 0644 compose/production/systemd/onerep-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now onerep-backup.timer
systemctl list-timers onerep-backup.timer
sudo systemctl start onerep-backup.service
sudo journalctl -u onerep-backup.service --since today --no-pager
```

Run an isolated restore check after initialization and on a regular schedule:

```sh
sudo /opt/onerep/compose/production/scripts/restore-check.sh --env-file /opt/onerep/.env
sudo bash compose/production/scripts/test_backup_restore.sh
```

`restore-check.sh` restores the latest snapshot into a private temporary directory, starts a labeled PostgreSQL container with a temporary filesystem and no network, restores and queries both databases, and checks every media file against the saved manifest. It never mounts the live PostgreSQL or RustFS volumes and never writes to any RustFS bucket.

For disaster recovery, provision a new RustFS destination and create a new, empty bucket there; give the recovery bucket a distinct name. Create a temporary IAM policy scoped to only that bucket (`s3:ListBucket` on its bucket ARN and `s3:GetObject`/`s3:PutObject` on its object ARN), then create a temporary identity and attach only that policy. Give the pinned `rc` client an ephemeral alias for that identity and mirror the verified `media/` directory from the restored snapshot into the new bucket. Mirror the new bucket back to an empty local verification directory and run `sha256sum --check` against the restored `media.sha256` manifest before routing applications to the recovered destination. Disable and revoke the temporary identity, verify that its credentials are denied, and then remove its temporary policy. Keep the production bucket and credentials out of this procedure; never mirror a recovery into the live bucket or use `--remove`.

Do not store non-disposable media until the pilot go/no-go gate passes: observe representative use for seven days with no OOM kills or unexpected restarts; retain at least 512 MiB host `MemAvailable` at peak; keep at least 20% of data-volume capacity free after backup and restore tests; avoid CPU saturation above 80% for more than 15 minutes; and complete one successful off-server backup plus restore. Keep the Restic password and offsite provider credentials in the protected secret store so the encrypted repository can be recovered independently of this server.

## Pull request validation

The public validation workflow receives no production credentials. It checks the Compose model, workflow syntax, shell scripts, NGINX bootstrap configuration, and runs the RustFS bootstrap tests against a uniquely named disposable Compose project and volume set.
