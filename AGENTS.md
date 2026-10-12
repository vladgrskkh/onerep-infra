# onerep-infra

Docker Compose deployment, GitHub Actions workflows, and operating scripts for OneRep. This repository is public and uses standard GitHub-hosted runners.

## Read before editing

- Read this file before inspecting or changing the implementation.
- Read the shared [CONVENTIONS.md](https://github.com/vladgrskkh/gym/blob/main/CONVENTIONS.md), available locally at `/Users/peshka/projects/gym/CONVENTIONS.md`. It describes the backend interfaces this deployment consumes; infrastructure scripts follow their own language conventions.
- Keep all Superpowers plans, specs, ledgers, briefs, and review reports in the root `gym` workspace. Do not create `docs/superpowers` in this repository.

## Deployment contract

- One public hostname: `onerep.mooo.com`.
- NGINX strips `/auth/` and `/gym/` prefixes when proxying to the services.
- S3 uses `/gym-media/<key>` and preserves the signed Host, path, and query.
- One PostgreSQL instance, separate service databases and roles; migrations run independently as one-shot jobs.
- Database, Redis, RustFS, console, and backend ports remain private to Compose.
- Production deployment is manual and uses the protected GitHub `production` Environment.
- Pin upstream images to tested exact releases/digests and application images to immutable GHCR tags.

## Secrets and data

- Never commit real `.env` files, credentials, keys, certificates, media, or backup archives.
- Keep `.env.example` dummy-only. Never print secrets or enable tracing around secret handling.
- RustFS root credentials are for bootstrap/admin only. The API and backup identities have separate bucket-scoped policies.
- Restore and destructive test cleanup must use verified isolated destinations and unique Compose project names.

## Validation

Run the relevant checks before committing:

```sh
docker compose --env-file .env.example -f compose/production/compose.yaml config --quiet
python3 -m unittest discover -s compose/production/scripts -p 'test_*.py'
python3 -m unittest discover -s compose/production/tests -p 'test_*.py'
shellcheck compose/production/scripts/*.sh
git diff --check
```

CI also validates workflow syntax, NGINX configs, systemd units, and disposable backup/restore flows. Use `gh` subcommands for repository and PR operations. PR titles describe the resulting change as an informative commit message; descriptions use actual Markdown newlines and report validation accurately. Deployment code goes through a reviewed PR.
