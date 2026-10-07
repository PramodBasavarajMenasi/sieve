# Sieve — open-source test impact analysis for agent-driven CI

## What this project is
A self-hosted server + CLI + GitHub Action + MCP server that:
1. Records every CI test result (JUnit XML) into per-test history.
2. Given a code change, selects which tests need to run (with a safe full-suite fallback).
3. Tells humans and coding agents whether a failure is a NEW regression, PRE-EXISTING on main, or FLAKY.

Inspired by Anthropic's "Agentic coding is straining CI" post (Sept 2026). Think "Observal for CI".
License: Apache-2.0. "sieve" is a working name.

## Stack
- Python 3.11+, managed with `uv`
- FastAPI (API server), SQLAlchemy 2.0 + Alembic (Postgres), Pydantic v2
- Typer (CLI), official `mcp` Python SDK (MCP server)
- pytest for tests, ruff for lint/format, mypy for types
- Docker Compose for local stack (api + postgres)

## Repo layout
```
sieve/
  pyproject.toml
  docker-compose.yml
  alembic/                 # DB migrations
  src/sieve/
    api/                   # FastAPI app, routes
      main.py
      routes/runs.py       # POST /runs (ingest)
      routes/select.py     # GET /select (week 2)
    core/
      junit.py             # JUnit XML parser -> TestResult models
      models.py            # SQLAlchemy ORM models
      schemas.py           # Pydantic request/response schemas
      history.py           # per-test history rollup, flakiness, broken-on-main
      selector.py          # test selection (week 2)
    cli/main.py            # `sieve record`, `sieve select`
    mcp/server.py          # MCP tools (week 4)
    db.py                  # engine/session
    config.py              # settings via env vars
  scripts/backfill.py      # pull past GitHub Actions JUnit artifacts
  tests/
    fixtures/              # sample JUnit XML (pytest, jest, go, junit)
```

## Design rules (important)
- API workers are STATELESS. Never keep test history in process memory. All state goes to Postgres.
- Ingest is append-only: store raw runs, compute history from them (rollup), never mutate raw results.
- Selection must be CONSERVATIVE: if confidence is low, history is missing, or config/build files changed
  (pyproject.toml, package.json, Dockerfile, CI files, lockfiles), return the FULL suite.
- Test identity = `(repo, test_id)` where test_id = `classname::name` normalized.
- Every selection response includes a `reason` per test (for the `explain` feature).

## Data model (initial)
- `repos(id, name, created_at)`
- `runs(id, repo_id, commit_sha, branch, is_main, ci_run_id, started_at, created_at)`
- `changed_files(run_id, path)`
- `test_results(id, run_id, test_id, file_path, status[passed|failed|skipped|error], duration_ms, attempt)`
- `test_stats(repo_id, test_id, runs, failures, last_failed_at, flaky_score, broken_on_main_since_sha)` — rollup table

Flaky = same commit_sha has both a failed and a passed result for a test.
Broken on main = test's most recent results on main branch are failures; record first failing sha.

## Current milestone: WEEK 1
- [ ] Project scaffold (uv, ruff, pytest, docker-compose with postgres)
- [ ] JUnit XML parser supporting pytest, Jest (jest-junit), Go (go-junit-report), Maven/Gradle output
- [ ] SQLAlchemy models + first Alembic migration
- [ ] `POST /runs` — multipart: junit file(s) + JSON metadata (repo, commit_sha, branch, changed_files)
- [ ] History rollup (recompute test_stats for affected tests after ingest)
- [ ] `GET /tests/{test_id}/history` for debugging
- [ ] `scripts/backfill.py` — use GitHub API to download JUnit artifacts from past workflow runs
- [ ] Tests with fixtures for every parser format; >80% coverage on core/

Later weeks: selector (2), GitHub Action + PR comment (3), flaky/pre-existing + MCP (4),
recall harness (5), docs + launch (6). Don't build these until asked.

## Conventions
- Small, focused commits. Run `ruff check`, `ruff format`, `mypy src`, `pytest` before finishing a task.
- Type hints everywhere. No bare `except`.
- Config only via env vars (`SIEVE_DATABASE_URL`, `SIEVE_API_TOKEN`).
- Write a test for every bug fix.