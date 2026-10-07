# sieve

Open-source test impact analysis for agent-driven CI.

## How it works

1. **Record.** CI uploads its JUnit XML to `POST /runs` along with run metadata: repo, commit,
   branch, CI run id and attempt, and the files the commit changed. pytest, Jest (jest-junit),
   Go (go-junit-report) and Maven/Gradle reports are supported. Each retry attempt is stored as
   its own result. Uploads are idempotent per CI run attempt, and `GET /runs/lookup` reports
   whether a run is already stored.
2. **Roll up.** In the same transaction, sieve recomputes per-test stats for every test in the
   run: run and failure counts, last failure, average duration, a flaky score, and whether the
   test is currently broken on the main branch (with the commit where it started failing).
   Stats are rebuilt from raw results rather than incremented, so the order runs arrive in
   doesn't change the result.
3. **Inspect.** `GET /tests/{test_id}/history?repo=` returns a test's stats and its last 20
   results.

Test selection for a given change is the next milestone. When sieve isn't confident, it will
fall back to running the full suite.

Raw results are append-only. All state lives in Postgres, so API workers are stateless. Every
endpoint except `/healthz` and the API docs requires `Authorization: Bearer $SIEVE_API_TOKEN`.

## Development

```sh
uv sync                       # install deps (incl. dev group)
docker compose up -d postgres # local Postgres on :5432
cp .env.example .env

uv run alembic upgrade head
uv run uvicorn sieve.api.main:app --reload

uv run ruff check && uv run ruff format --check
uv run mypy src tests scripts
uv run pytest
```

Database tests run against `SIEVE_TEST_DATABASE_URL` and are skipped when it is unset. Compose
creates a `sieve_test` database on first start:

```sh
export SIEVE_TEST_DATABASE_URL=postgresql+psycopg://sieve:sieve@localhost:5432/sieve_test
```

Full stack (api + postgres): `docker compose up --build`.

## Backfill

Seed history from a repo's past GitHub Actions runs. Each completed run's JUnit artifacts are
downloaded, their `*.xml` files are uploaded to `POST /runs`, and changed files are taken from
the GitHub compare API (first parent → head).

```sh
export GITHUB_TOKEN=...      # needs actions:read and contents:read on the repo
export SIEVE_API_TOKEN=...
uv run python scripts/backfill.py --repo acme/shop --server http://localhost:8000 \
    --workflow ci.yml --artifact-pattern '*junit*' --max-runs 200
```

- Safe to re-run. Each run is checked with `GET /runs/lookup` first, and runs sieve already
  has are reported as `skipped` without downloading anything. The script ends with a summary
  (`new / skipped / no artifacts / expired / errors`) and exits 1 if any run errored.
- GitHub keeps artifacts for 90 days by default, so older runs show up as `expired`.
- Waits out GitHub rate limits and retries transient errors.
- `is_main` is true only for non-pull-request runs on the repo's default branch.
- Only the latest attempt of each run is backfilled.
- If the changed files can't be determined, the run is uploaded with `changed_files_known:
  false`. That covers a failed compare, a root commit, or more than 300 changed files. Test
  selection will run the full suite for such runs.

License: Apache-2.0
