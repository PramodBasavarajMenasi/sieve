<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img alt="sieve" src="assets/logo-light.svg" width="290">
  </picture>
</p>

<p align="center">
  <strong>Run only the tests your change needs, safely.</strong><br>
  Self-hosted test selection for agent-driven CI.
</p>

<p align="center">
  <a href="https://github.com/PramodBasavarajMenasi/sieve/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/PramodBasavarajMenasi/sieve/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-orange">
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#results-on-real-repos">Results</a> ·
  <a href="#roadmap">Roadmap</a>
</p>

<p align="center">
  <b>2,050 / 2,050</b> real failures caught on rdflib · <b><code>/select</code> in ~0.3 s</b> on 18,000 tests · <b>self-hosted</b>, Apache-2.0
</p>

<p align="center"><sub>If sieve looks useful, a ⭐ helps others find it.</sub></p>

> **Alpha, early development.** The server, CLI and test selection work, and have been evaluated
> on two real repos. The GitHub Action, MCP server and failure triage don't exist yet. APIs and
> the database schema may change.

## The problem

Coding agents multiply pull requests and CI runs. Running the full suite on every push wastes
time and money, and an agent can't tell whether a red test is its own failure, already broken
on main, or flaky. See Anthropic's [Agentic coding is straining CI](https://claude.com/blog/agentic-coding-is-straining-ci-heres-how-we-scaled-test-impact-analysis-at-anthropic)
and Linear's [AI coding has made CI a bottleneck](https://linear.app/now/ci-bottleneck-reworked).

## How it works

```mermaid
flowchart LR
    CI["CI run<br/>(JUnit XML)"] -->|POST /runs| S["sieve server"]
    S --> DB[("Postgres<br/>per-test history")]
    PR["PR or agent branch"] -->|"sieve select<br/>(git diff + import graph)"| S
    S -->|"test command + reasons"| T["pytest / go test / jest"]
```

<p align="center">
  <img alt="sieve select on an rdflib commit: 325 test files import the changed modules, 11448 of 11516 known tests selected, and a pytest command" src="assets/demo.svg" width="900">
</p>

- **Record.** CI uploads JUnit XML with the commit, branch and changed files. sieve keeps every
  result and rolls up per-test history: failures, average duration, a flaky score, broken on main.
- **Select.** `sieve select` diffs your branch, builds a Go or Python import graph locally, and
  asks the server which tests the change can affect.
- **Fall back safely.** Unknown diff, changed build/CI/dependency files, no history, or a changed
  file no signal covers → the full suite. Missing a failure is worse than running extra tests.
- **Explain.** Every selected test lists its reasons, e.g. `imports changed module rdflib/term.py`
  or `co-change: failed in run 412 (3f2a9c1), which also changed src/cart.py`.

## Results on real repos

Replays of real CI history with no peeking at the future. Full details: [docs/eval-results.md](docs/eval-results.md).

| Repo | Failures caught | Test time skipped |
|---|---|---|
| [ipfs/kubo](https://github.com/ipfs/kubo) (Go, ~4,100 tests) | 19 of 19 in failing PRs | 30.7% over 20 main commits replayed as PRs; 0% on recent PRs (all dependency/CI bumps) |
| [RDFLib/rdflib](https://github.com/RDFLib/rdflib) (Python, ~18,000 tests) | 2,050 of 2,050 in 39 failing PRs; 140 of 140 in selective runs | 0.2–0.6% |

rdflib saves almost nothing: its `__init__.py` and plugin registry connect nearly every module
to nearly every test file, so safe file-level selection still runs ~99% of the suite. Bigger
savings need codebases with independent parts (monorepos), or per-test coverage.

## Quickstart

Start the server with Postgres. The API token is a shared secret you choose:

```sh
git clone https://github.com/PramodBasavarajMenasi/sieve.git
cd sieve
export SIEVE_API_TOKEN=$(openssl rand -hex 32)
docker compose up -d --build
curl http://localhost:8000/healthz        # {"status":"ok"}
```

Seed history from a repo's past GitHub Actions runs (here rdflib, which uploads one JUnit
artifact per matrix leg). `GITHUB_TOKEN` needs `actions:read` and `contents:read`:

```sh
export GITHUB_TOKEN=...
uv run python scripts/backfill.py --repo RDFLib/rdflib --workflow validate.yaml \
    --artifact-pattern '*-pytest-junit-xml' --max-runs 20 --batch
```

Install the CLI and ask for a test command in a checkout of that repo:

```sh
uv tool install .                          # puts `sieve` on your PATH
cd .. && git clone https://github.com/RDFLib/rdflib.git && cd rdflib
sieve select --repo RDFLib/rdflib --base HEAD~1
```

The command goes to stdout (so `eval "$(sieve select ...)"` works) and a summary to stderr:

```text
sieve: python imports: 325 test file(s) import changed modules
sieve: selective (11448 of 11516 known tests): 11448 of 11516 known tests selected
pytest test/data/suites/trix/test_trix.py test/jsonld/test_api.py ...
```

`--json` prints the full response with per-test reasons. Exit codes: 0 ok, 1 API error, 2 usage
error, 3 full suite needed but no command known. In CI, check out with `fetch-depth: 0` so the
`base...head` diff works; if it fails, the full suite runs.

### Record results from CI

Until the GitHub Action exists, upload JUnit XML after each test run:

```sh
cat > meta.json <<EOF
{"repo": "acme/shop", "commit_sha": "$(git rev-parse HEAD)", "branch": "feature-x",
 "is_main": false, "ci_run_id": "1234", "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "changed_files": ["src/cart.py"]}
EOF
curl -X POST http://localhost:8000/runs -H "Authorization: Bearer $SIEVE_API_TOKEN" \
    -F files=@junit.xml -F "metadata=<meta.json"
```

Uploads are idempotent per `(repo, ci_run_id, run_attempt, variant)`: a re-upload returns 200
and stores nothing new. Set `variant` (e.g. `ubuntu-py3.12`) for each matrix leg.

## Features

- ✅ JUnit XML ingest: pytest, Jest (jest-junit), Go (go-junit-report), Maven/Gradle
- ✅ Per-test history: failures, durations, flaky score, broken on main (per matrix variant)
- ✅ Backfill from GitHub Actions artifacts, with whole-PR diffs
- ✅ `sieve select` CLI and `POST /select` API with a reason for every test
- ✅ Go import graph (`go list`) and static Python import graph
- ✅ Safe full-suite fallbacks, `.sieve.toml` for dependencies no graph can see
- ✅ Self-hosted: one FastAPI service and Postgres, stateless workers
- 🔜 GitHub Action (record + select) and PR summary comment
- 🔜 MCP server, so coding agents can ask which tests to run and why a test failed
- 🔜 Failure triage: new regression vs pre-existing on main vs flaky

## How it compares

From each project's public docs as of October 2026; corrections welcome.

| | Open source | Self-hosted | Languages | How it selects | Agent/MCP support |
|---|---|---|---|---|---|
| **sieve** | Yes (Apache-2.0) | Yes | Python, Go, JS/TS | file mapping, Go/Python import graphs, failure history | Planned |
| [CloudBees Smart Tests](https://docs.cloudbees.com/docs/cloudbees-smart-tests/latest/features/predictive-test-selection) | No | Not stated | Many (runner integrations incl. Python, Go, Java, JS) | LLM analysis of changes + test history | Not documented |
| [Datadog Test Impact Analysis](https://docs.datadoghq.com/tests/test_impact_analysis/) | No | No (Datadog SaaS) | JS/TS, Java, Python, Go, Swift and more | per-test code coverage | Not documented |
| [Develocity Predictive Test Selection](https://docs.develocity.ai/current/using-develocity/predictive-test-selection/) | No | Yes (Develocity server) | Gradle and Maven builds | predictive model from past builds | Not documented |
| [pytest-testmon](https://pypi.org/project/pytest-testmon/) | Yes (MIT) | Local plugin, no server | Python (pytest) | per-test code coverage | No |

Coverage-based tools see dynamic dependencies that static graphs miss, but they need instrumented
runs. sieve uses no instrumentation; when its graphs can't see a dependency, the fallbacks and
`.sieve.toml` cover it.

## Configuration

`.sieve.toml` at the repo root, read by `sieve select`:

```toml
always_run = ["tests/smoke/**"]           # always selected

[[depends]]                               # dependencies no import graph can see
tests = "test/cli/**"                     # test file paths, or <Go package dir>/<TestName>
on = ["**/*.go", "!**/*_test.go"]         # ** spans directories; ! excludes
```

| Variable | Used by | Default |
|---|---|---|
| `SIEVE_API_TOKEN` | server (required: if unset, every endpoint but `/healthz` returns 503) and clients | none |
| `SIEVE_DATABASE_URL` | server | `postgresql+psycopg://sieve:sieve@localhost:5432/sieve` |
| `SIEVE_STATS_WINDOW_DAYS` | server: per-test stats window (0 = all history) | `90` |
| `SIEVE_MAX_UPLOAD_BYTES` | server: request body limit | 50 MB |
| `GITHUB_TOKEN` | `scripts/backfill.py` | none |

## Supported languages

| Language | Selection | Commands |
|---|---|---|
| **Python** (pytest) | `foo.py` → `test_foo.py`; test files importing a changed module (static graph incl. parent packages, conftests, plugin strings); changed test files | `pytest` node IDs, whole test files, `pytest --doctest-modules` for doctests |
| **Go** | changed package and every package whose tests import it (`go list -deps -test`) | `go test ./pkg` (whole) or `-run '^(TestA\|TestB)$'` |
| **JS/TS** (Jest) | `foo.ts` → `foo.test.*`, `foo.spec.*`, `__tests__/foo.*` (needs file paths in the JUnit) | `jest <files>` |
| **Java** (Maven/Gradle) | recorded with full history; selection falls back to the full suite | — |

## Development

```sh
uv sync                                   # install deps, incl. the dev group
docker compose up -d postgres             # Postgres on :5432, plus a sieve_test database
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn sieve.api.main:app --reload

uv run ruff check && uv run ruff format --check
uv run mypy src tests scripts
SIEVE_TEST_DATABASE_URL=postgresql+psycopg://sieve:sieve@localhost:5432/sieve_test uv run pytest
```

Database tests are skipped when `SIEVE_TEST_DATABASE_URL` is unset. After upgrading an existing
database past migration 0005, run `POST /repos/{repo}/rollup` once per repo. Replay the selector
on a backfilled repo with `uv run python -m scripts.eval.run_eval --repo owner/name --checkout <clone>`.

## Roadmap

- **Next:** smaller storage, faster rollups, a monorepo eval, the GitHub Action with a PR
  summary comment.
- **Then:** flaky and pre-existing failure triage, and an MCP server for coding agents.
- **Later:** a recall harness, docs and launch.

## Contributing

Issues and pull requests are welcome. Keep changes small, add a test for every bug fix, and run
ruff, mypy and pytest before opening a PR.

## License

Apache-2.0
