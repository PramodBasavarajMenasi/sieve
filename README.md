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
4. **Select.** `sieve select` (or `POST /select`) turns a change into a test command. Each
   selected test comes with its reasons. It runs the full suite instead whenever it isn't
   confident: unknown or empty diff, build/CI/dependency files changed, no history, or a
   changed file nothing covers.

## Selecting tests

```sh
export SIEVE_API_TOKEN=...
eval "$(sieve select --repo acme/shop --base origin/main --head HEAD --server https://sieve.example)"
```

The command goes to stdout and a one-line summary to stderr. `--json` prints the full
response. In CI, fetch enough history for `base...head` (e.g. `fetch-depth: 0`); if the diff
fails, the full suite runs.

Tests are selected from:

- **The changed files' own tests.** Python `foo.py` selects tests in `test_foo.py`. JS/TS
  `foo.ts` selects `foo.test.*`, `foo.spec.*` and `__tests__/foo.*`. A changed Go file runs its
  whole package. A changed test file runs itself.
- **Go dependents.** In a Go module the CLI runs `go list -deps -test -json ./...` and selects
  every package whose tests import a changed package, directly or transitively. If `go list`
  fails, this is skipped; use `--no-go-list` to skip it on purpose.
- **History.** Tests that failed in earlier runs touching the same files (co-change), and
  tests that recently failed or are broken on main.
- **`.sieve.toml`** at the repo root, for dependencies sieve can't see. An example is a test
  suite that drives a built binary:

  ```toml
  always_run = ["tests/smoke/**"]

  [[depends]]
  tests = "test/cli/**"                # test file paths, or <Go package dir>/<TestName>
  on = ["**/*.go", "!**/*_test.go"]    # ** spans directories; ! excludes
  ```

Raw results are append-only. All state lives in Postgres, so API workers are stateless. Every
endpoint except `/healthz` and the API docs requires `Authorization: Bearer $SIEVE_API_TOKEN`.

## Development

```sh
uv sync                       # install deps (incl. dev group)
docker compose up -d postgres # local Postgres on :5432
cp .env.example .env

uv run alembic upgrade head
uv run uvicorn sieve.api.main:app --reload
```

Upgrading an existing database past migration 0005: run `POST /repos/{repo}/rollup` for each
repo afterwards. `/select` reads known tests from `test_stats`, so until the rollup fills the
new columns the repo has no known tests and `/select` returns the full suite.

```sh

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
- Each matching artifact is uploaded as its own run, a *variant* (e.g. `3.12-ubuntu-latest`
  for a matrix leg). Results are only compared within a variant, so a test that passes on
  Linux and fails on macOS isn't counted as flaky.
- **`--batch`** for large backfills: uploads skip the per-run stats update
  (`POST /runs?defer_rollup=true`) and the script calls `POST /repos/{repo}/rollup` once at
  the end. Per-test stats (and the selector's broken-on-main signal) are **stale until that
  rollup finishes**. If the script is interrupted, run the rollup yourself:
  `curl -X POST -H "Authorization: Bearer $SIEVE_API_TOKEN" $SERVER/repos/owner/name/rollup`.

### Stats window

Per-test stats are computed over the last `SIEVE_STATS_WINDOW_DAYS` days (default 90; `0` =
all history), counted back from the repo's most recent run. Each ingest only reads results
inside that window, so its cost doesn't grow with how much history is stored. Broken-on-main
is the exception: when a test has failed on main for the whole window, older main results are
read to find where its failing streak started.

License: Apache-2.0
