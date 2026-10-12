# OneRep infrastructure

Public deployment configuration and operating runbooks for OneRep.

The first deployment uses Docker Compose, NGINX, PostgreSQL, Redis, and RustFS on one server. Its public hostname is `onerep.mooo.com`; the mobile client uses the `/auth` and `/gym` API prefixes.

Deployment is manually triggered through GitHub Actions. Runtime credentials belong in the GitHub `production` Environment and an untracked server `.env` file. This repository contains configuration and dummy examples only.

The approved [deployment design](https://github.com/vladgrskkh/gym/blob/main/docs/superpowers/specs/2026-10-12-compose-deployment-design.md) and [implementation plan](https://github.com/vladgrskkh/gym/blob/main/docs/superpowers/plans/2026-10-12-compose-deployment.md) live in the `gym` repository.
