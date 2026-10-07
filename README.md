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
uv run mypy src
uv run pytest
```

Full stack (api + postgres): `docker compose up --build`.

License: Apache-2.0
