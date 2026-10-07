# sieve

Open-source test impact analysis for agent-driven CI. See `CLAUDE.md` for design notes.

## Development

```sh
uv sync                       # install deps (incl. dev group)
docker compose up -d postgres # local Postgres on :5432
cp .env.example .env

uv run alembic upgrade head
uv run uvicorn sieve.api.main:app --reload

uv run ruff check && uv run ruff format --check
uv run mypy src tests
uv run pytest
```

Database tests run against `SIEVE_TEST_DATABASE_URL` and are skipped when it is unset. Compose
creates a `sieve_test` database on first start:

```sh
export SIEVE_TEST_DATABASE_URL=postgresql+psycopg://sieve:sieve@localhost:5432/sieve_test
```

Full stack (api + postgres): `docker compose up --build`.

License: Apache-2.0
