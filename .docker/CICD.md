# CI/CD Guide

## Pipeline Overview

Workflow files:
- `.github/workflows/main.yml` — CI and the GHCR image publish, on `main`.
- `.github/workflows/Dev_CI.yml` — the CI that gates `dev` (push and PR).
- `.github/workflows/deploy.yml` — deploys to ECS. Separate workflow, separate
  registry (ECR), separate trigger.

`main.yml` is `main`-only by design; `dev` is covered by `Dev_CI.yml`. Both
branches are therefore gated, just by different workflows.

Triggers for `main.yml`:
- `pull_request` to `main`: runs CI, then CD build validation (no push).
- `push` to `main`: runs CI, then builds and publishes to GHCR.

Execution order on `push`:
1. CI matrix: `python 3.11`, `python 3.12`
2. Docker build and push to GHCR

CD is gated by CI using `needs: ci`.

`main.yml` does not deploy. It publishes an image to GHCR only.

## CI Stages

CI runs these checks:
- Install dependencies
- Ruff lint (`E9,F63,F7,F82`)
- Unit tests: `-m "not db and not api"`
- DB integration tests (`-m "db"`) on `3.11`, against a `postgres:16-alpine`
  service container — see below
- Per-file coverage gate at `50%` on selected production-critical modules
- Upload `coverage.json` artifact per Python version

### The `db` lane

`db`-marked tests run against a throwaway Postgres service container, not the
real database. `mqsmaster-prod-postgres` is `PubliclyAccessible=false`, so a
GitHub-hosted runner has no route into that VPC — the previous secrets-based
lane failed at DNS (`could not translate host name`) on every run.

Two things to know if you touch it:
- `sslmode` must be `disable`. `MQSDBConnector` defaults it to `require` and the
  container serves plaintext.
- `SchemaDefinitions.create_all_tables()` returns early on a connection error,
  `pass`es on a failed statement, and its `__main__` wrapper catches everything,
  so it exits `0` regardless of what happened. The bootstrap step asserts the
  ten expected tables exist rather than trusting that exit code.

`services:` is job-level, so the container also starts on the `3.12` leg, which
does not use it.

## CD Stages

CD runs after CI in both trigger types:
- PR: Docker build validation only
- Push to `main`: build and publish to GHCR

CD steps:
- Build Docker image from repo root `Dockerfile`
- On push to `main`: push image to GHCR with `latest` and `sha` tags

## Deployment (`deploy.yml`)

Deployment to AWS ECS is a separate workflow. It builds its own image and pushes
it to **ECR** (`livetradingbot`), not GHCR, because the ECS task definitions pull
from ECR over a VPC S3 gateway endpoint and need no registry pull credentials.

It updates one workload in `MUNQuantSociety/MQS_AWS_INFRA`:
- **Market task** (`mqsmaster-prod`) — registers a new task definition revision.
  The `mqsmaster-prod-market-open` schedule (`cron(0 11 ? * MON-FRI *)`,
  `America/St_Johns` ≈ 09:30 ET) targets the family with **no revision pinned**,
  so the next weekday run picks the image up on its own. Nothing is rolled, and
  a deploy landing after 09:30 ET does not take effect until the next weekday.

An NLP service (`mqsmaster-prod-nlp`) was targeted here previously. It does not
exist in the account — the cluster runs no services at all — and the steps were
removed. Because they ran after the ECR push and the market register, every run
half-applied. Restoring them needs the task definition, the service, an
`/ecs/mqsmaster-prod-nlp` log group, and `ecs:UpdateService` +
`ecs:DescribeServices` on the deploy role (plus `ecs:DescribeTasks` /
`ecs:ListTasks` if `wait-for-service-stability` is kept).

### Trigger

`workflow_run` on **"CI/CD trading sys"** (`main.yml`) completing, plus
`workflow_dispatch` for manual redeploys and rollbacks. A prod image is
therefore only built from a commit that passed lint, both matrix legs, the db
lane, and the Docker build.

Three `workflow_run` details this file depends on:
- It also fires for `main.yml`'s **scheduled** runs, whose `head_branch` is also
  `main`. The job guard requires `workflow_run.event == 'push'`, so the
  six-hourly synthetic-monitoring pass cannot trigger a deploy.
- The event runs against the **default branch**, so `GITHUB_SHA` is main's head
  rather than the commit CI validated. The job resolves
  `workflow_run.head_sha` and both checks out and tags from that.
- `github.ref` is `refs/heads/main`, which is what the role's trust policy
  expects.

### Auth

GitHub OIDC. The job assumes `mqsmaster-prod-github-deploy`; there are no
long-lived AWS keys in this repo. The trust policy pins the OIDC subject to
`repo:MUNQuantSociety/MQSMaster:ref:refs/heads/main` and `…:ref:refs/heads/dev`,
so a dispatch from any other branch is rejected by STS. The deploy job must also
**not** declare a job-level `environment:` — that changes the subject claim to
the environment form and STS rejects the assume-role call.

Images are tagged with the commit SHA only, never `latest`. The ECR lifecycle
policy keeps the last 10 tagged images and expires untagged ones after 7 days,
so rollback depth is about 10 deploys — older task definition revisions will
reference images that no longer exist.

The previous `DEPLOY_WEBHOOK_URL` path and `manual-deploy.yml` were removed —
they posted to a webhook receiver that no longer exists.

## Required Secrets

Actually required:
- `AWS_DEPLOY_ROLE_ARN` — deploy only (`terraform output -raw github_deploy_role_arn`)
- `FMP_API_KEY` — the `db`/`api` integration lane and synthetic monitoring

No longer used by `main.yml`: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `DB_SSLMODE`. The `db` lane runs against a service container now.
`Dev_CI.yml` still passes the `DB_*` secrets into its pytest env, but its
selector is `-m "smoke and workflow_backtest"`, which excludes `db`, so they go
unused — adding a `db`-marked test to that selection would fail against the
private RDS endpoint. Give `Dev_CI.yml` the same service container before doing
that, rather than restoring the secrets path.

## Local Test Commands

Use PowerShell from repo root.

1. Setup environment and tools:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pip install ruff pytest pytest-cov pip-audit
```

2. Lint:

```powershell
python -m ruff check src tests scripts --select E9,F63,F7,F82
```

3. Unit tests + coverage JSON:

```powershell
python -m pytest -q -m "not db and not api" --cov=src --cov-report=term-missing --cov-report=json
```

4. Per-file coverage gate (same scope as CI):

```powershell
python scripts/check_per_file_coverage.py --min 50 --coverage-file coverage.json --include-glob "src/backtest/*.py" --include-glob "src/common/auth/*.py" --include-glob "src/orchestrator/marketData/fmpMarketData.py" --include-glob "src/portfolios/indicators/*.py" --include-glob "src/portfolios/order_interface.py" --include-glob "src/main_backtest.py"
```

5. DB integration tests (if secrets configured):

```powershell
python -m pytest -q -m "db" --cov=src --cov-append --cov-report=term-missing --cov-report=json
```

6. API integration tests (if key configured):

```powershell
python -m pytest -q -m "api" --cov=src --cov-append --cov-report=term-missing --cov-report=json
```

7. Optional dependency security scan:

```powershell
python -m pip_audit -r requirements.txt
```

## Per-File Coverage Gate Scope

The per-file gate intentionally targets tested, production-critical modules and enforces a hard floor of `50%`:
- `src/backtest/*.py`
- `src/common/auth/*.py`
- `src/orchestrator/marketData/fmpMarketData.py`
- `src/portfolios/indicators/*.py`
- `src/portfolios/order_interface.py`
- `src/main_backtest.py`

As additional modules get tests, add them to the include list in `.github/workflows/main.yml`.

## Action Pinning Policy

All workflow actions are pinned to commit SHAs.

Maintenance:
- Keep source-tag comments next to each SHA.
- Refresh pins monthly or immediately for security advisories.
- Validate updates with full CI before merge.

## Latest Validation Notes

Most recent local CI-like run summary:
- Lint (`ruff`): passed.
- Unit test run (`-m "not db and not api"`): `39 passed`.
- Scoped per-file coverage gate (50% on selected critical modules): passed.
- Total repository coverage from the same run: `33%` (not a failing gate currently).
- DB/API integration tests: not executed in that run (missing local secrets context).

Interpretation:
- Pass rate for executed tests is good (`100%` of executed tests passed).
- Overall project test health is mixed, because broad repository coverage is still low and integration lanes were not exercised in that local run.
