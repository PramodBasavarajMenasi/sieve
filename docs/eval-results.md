# Selector evaluation

Replays of siftwise's test selector against real CI history, to find out how much test time
selection saves and whether it would have missed real failures:

- [ipfs/kubo](#ipfskubo): a Go project with about 4,100 known tests.
- [RDFLib/rdflib](#rdflibrdflib): a Python project with about 18,000 known tests. Each CI run
  uploads one JUnit report per matrix leg (operating system × Python version), about 16 legs
  per run.

## ipfs/kubo

### Summary

- **Every real failure was caught in every round:** 19 of 19 in the final round. In each case the
  run fell back to the full suite. Not one failing run went selective, so recall on a selective
  run with failures is still unmeasured.
- **The Go import graph closed the dependents gap.** Counting only the selector's own signals, with
  the full-suite fallbacks switched off, the failing PRs went from **1 of 9** failures caught
  (path mapping alone) to **9 of 10**. The one remaining miss was protected by a `go.mod`
  fallback in the same change, and co-change caught it when the PR was re-pushed.
- **Savings on kubo are modest:** an estimated **30.7% of test time** across 20 main commits
  replayed as PRs.
  - 13 of the 20 commits ran the full suite, almost all because of dependency or CI file changes.
  - Of the 7 selective commits, 6 were docs-only and skipped everything. The only selective code
    change skipped 14% of test time.
- **One kubo package dominates test time.** `test/cli` runs the built `ipfs` binary. It holds 48%
  of kubo's tests but about 85% of their runtime (11.6 of 13.7 minutes, counting top-level
  tests). Its test harness imports 61 kubo packages, so most code changes reach it through the
  import graph. That's correct, but it means code changes save only 10–20% of time, not 90%.

### Method

Code: `scripts/eval/run_eval.py`. `scripts/eval_kubo.py` is a kubo preset for it.

- **No peeking.** For each target run, the selector's history is rebuilt from raw results stored
  strictly before that run, ordered by run time.
- **What runs.** A failure counts as caught if the selection would have run it. In full mode
  everything runs. A Go package that is run whole runs all its tests, including new ones. A
  `-run` filter on a top-level test also runs that test's subtests.
- **"Without fallbacks"** is a counterfactual. It asks which failures the selector's own signals
  would have caught if the full-suite fallbacks were switched off. The signals are path mapping,
  the Go import graph, declared dependencies, co-change and recently failed. Runs where nothing
  maps on its own are counted separately: there, only a fallback protects the run.
- **Time skipped** is estimated from each test's average duration in history. A full-suite run
  counts as 0%.
- **Import graph.** The Go import graph is computed with `go list -deps -test -json ./...` at
  each target commit.
- **Two modes:**
  - **PR mode** replays the 10 most recent PR runs, plus every PR run that had failures (`--failing`).
  - **`--replay-main`** treats each main-branch run as a PR. Its diff is the previous main commit
    compared with this one, fetched from GitHub.
- **Data:** about 100 backfilled GitHub Actions runs, from 2026-07-29 to 2026-10-01. The first
  backfill stored 107 runs and 432K results. After the backfill was changed to record whole-PR
  diffs and re-run, the final data set had 65 PR runs and 21 main runs, all with known diffs.

### Round 1: path mapping only

Baseline selector: path mapping, co-change, recently failed and always-run. Backfill recorded
each run's diff as head commit versus first parent.

**10 most recent PR runs:** 7 full, 3 selective. 6 of the full runs were for build or CI files,
and 1 for a file that mapped to no tests. The selective runs skipped 99.6% of tests on average.
All 9 failures were caught.

**PR runs with failures:** 6 runs and 22 failures. All 6 ran the full suite, so all 22 failures
were caught.

| Run | What changed | Failed tests | Without fallbacks | Signal that would catch it |
|---|---|---|---|---|
| a41db4a | `go.mod`, `go.sum` (dependabot) | 9 routing tests in `test/cli` | nothing maps; only the fallback protects it | build-file fallback |
| 6badb4a | `config/import.go`, `core/commands/dag/import.go` | 4 × `test/cli::TestCidCommands…` | selects `./config` (260 tests), **misses 4** | dependency on the binary: `test/cli` links both packages |
| ef7060f | `plugin/plugins/wasmipld/wasmipld.go` | the same 4 tests | nothing maps; only the fallback protects it | same |
| 9446212 | `core/core_test.go`, `go.mod`, `go.sum` | `test/cli::TestBackupBootstrapPeers` | selects `./core` (13 tests), **misses 1** | same |
| 5931539 | the same change, re-pushed | the same test | **caught** (15 tests) | co-change: it failed in the previous push |
| d0857a8 | 40 files | 3 × `ondemandpin::…` (new tests) | **misses 3** | none: the package isn't in the recorded diff (a backfill bug, see round 2) |

Without fallbacks, path mapping caught **1 of 9** failures in the runs where it applied. The other
13 failures had only a fallback protecting them.

### Round 2: whole-PR diffs, changed Go packages run whole

Two changes went into this round:
- Backfill now diffs the PR base against the head for `pull_request` runs, and `before...after`
  for pushes.
- A Go package with changed `.go` files runs without a `-run` filter, so new tests in it execute.

| | Round 1 (head vs first parent) | Round 2 (whole PR or push) |
|---|---|---|
| **10 most recent PR runs** | | |
| Full / selective | 7 / 3 | 10 / 0 |
| Full because of build or CI files | 6 | 10 |
| Full because a file mapped to no tests | 1 | 0 |
| Failures caught | 9 / 9 | 9 / 9 |
| **PR runs with failures and a known diff** | 6 runs, 22 / 22 caught | 3 runs, 11 / 11 caught |
| Caught without fallbacks | 1 / 9 | 1 / 2 |

The numbers got less favourable, and that's correct. The old 99.6% came from release-branch runs
whose recorded diff was a single commit (`version.go`). Their real PR diff has 75 to 76 files,
including CI workflows.

The number of failing runs dropped from 6 to 3 for two reasons:
- GitHub hadn't linked the `TestCidCommands` runs to a PR, so their diffs became unknown. The
  selector runs the full suite for an unknown diff, but the eval skips such runs.
- The `ondemandpin` run was no longer among the 100 newest runs on GitHub.

The `-run` change had no effect on this sample, because no failing run went selective.

### Round 3: Go import graph, declared dependencies, PR lookup fallback

Three changes went into this round:
- **PR lookup fallback:** backfill finds a run's PR from the repo's PR list by head branch,
  including closed PRs. Unknown PR diffs went from 18 to 0.
- **Import graph:** `siftwise select` runs `go list` and sends `affected_packages`. Every package
  whose test build imports a changed package is selected and run whole.
- **Declared dependencies:** `.siftwise.toml` `[[depends]]`. kubo's rule
  ([scripts/eval/kubo.siftwise.toml](../scripts/eval/kubo.siftwise.toml)) makes `test/cli` depend on
  every non-test `.go` file.

#### PR mode

| | Round 2 | Round 3 |
|---|---|---|
| 10 most recent PR runs | 10 full | 10 full (all for build or CI file changes) |
| Failing PR runs with a known diff | 3 runs, 11 failures, all caught | 5 runs, 19 failures, all caught |
| **Caught without fallbacks** | **1 of 2** | **9 of 10** |

| Run | What changed | Failed tests | Without fallbacks (import graph + `.siftwise.toml`) |
|---|---|---|---|
| a41db4a | `go.mod`, `go.sum` | 9 routing tests | nothing maps; only the fallback protects it |
| 6badb4a | 11 files incl. `config/import.go`, `core/commands/dag/import.go`, `go.mod` | 4 × `TestCidCommands…` | 4,020 tests, **catches 4 / 4** (go imports) |
| ef7060f | 9 files incl. `plugin/loader/loader.go`, `go.mod` | the same 4 tests | 3,385 tests, **catches 4 / 4** (go imports) |
| 5931539 | `core/core_test.go`, `go.mod`, `go.sum` | `TestBackupBootstrapPeers` | 15 tests, **catches 1 / 1** (co-change) |
| 9446212 | the same change, first push | the same test | 13 tests, **misses 1** |

#### The import-graph improvement

Counting only the selector's own signals, failures caught went from **1 of 9** with path mapping
alone (round 1) to **9 of 10** (round 3).

- **The import graph caught the `TestCidCommands` failures.** In round 1 they were the main
  example of the dependents gap. `test/cli`'s test build imports 61 kubo packages through its
  harness, so a change to `core/commands/dag` or the plugin loader reaches it through imports.
- **The declared rule added almost nothing for kubo.** With and without `kubo.siftwise.toml`, PR mode
  caught the same failures: 9 of 10. The rule only made selections slightly larger: 4,020 vs
  4,018 tests on 6badb4a, and 3,385 vs 3,237 on ef7060f. In the main replay it applied to one
  run (ceafeba): 1,865 → 2,013 tests and 19% → 14% of time skipped. That moved the overall
  estimate from 31.0% to 30.7%. It stays useful as a safety net for effects that pass through
  the binary.

#### Main replay (`--replay-main`, 20 main commits replayed as PRs)

| | |
|---|---|
| Full / selective | 13 (65%) / 7 (35%) |
| Full because of build or CI files | 9 (8 bumped `go.mod`/`go.sum`; 1 changed only a workflow file) |
| Full because a changed file mapped to no known tests | 3 (`fuse/*_test.go`, `test/sharness/t0025-datastores.sh`, `test/dependencies/pollEndpoint/main.go`) |
| Full because the diff was empty | 1 |
| Skipped in selective runs | 93.0% of tests, 87.7% of time on average |
| **Estimated time skipped across all runs** | **30.7%** (full runs count as 0%) |
| Signals used by selective runs | path mapping 1, go imports 1, declared 1 |
| Failures | none: main had no failing runs in this window |

- **Docs-only commits:** 6 of the 7 selective runs had no affected tests and skipped 100%.
- **The one code change** (ceafeba) selected 2,013 of 4,133 tests. That skipped 51% of tests but
  only 14% of time, because the selection included `test/cli`.
- **Nested build files:** a later change scoped nested build files such as
  `docs/examples/kubo-as-a-library/go.mod` to their own directory. The split stayed 13 / 7,
  because every commit that bumped the nested `go.mod` also bumped the root one.

### Misses

Every counterfactual miss across the rounds, and the signal that catches it:

| Failure | Change | Status now | Signal |
|---|---|---|---|
| 4 × `test/cli::TestCidCommands…` (6badb4a, ef7060f) | `config`, `core/commands/dag`, plugin loader | **caught** | Go import graph (round 3) |
| `test/cli::TestBackupBootstrapPeers` (9446212) | `core/core_test.go` + `go.mod` | **missed** without fallbacks | The `go.mod` build-file fallback in the same change already runs the full suite. The only code change is a test file in `./core`, which `test/cli` doesn't import, and the declared rule excludes `*_test.go`. Co-change caught it on the re-push (5931539). |
| 3 × `ondemandpin::…` (d0857a8) | 40 files, multi-commit PR | fixed in the data | A backfill bug, not a selector gap: the package was added earlier in the same PR, and only the head commit's diff was recorded. Whole-PR diffs (round 2) fix it. |
| 9 routing tests in `test/cli` (a41db4a) | `go.mod`, `go.sum` only | caught | The build-file fallback. No selector signal applies to a dependency-only bump, and the full suite is the right answer. |

### Findings

1. **Fallbacks did all the protecting.** Every failing run fell back to the full suite: the
   conservative rules work, but they also keep savings low. On kubo, 8 of the 13 full-suite runs in
   the main replay came from `go.mod`/`go.sum` bumps.
2. **Path mapping alone isn't safe for Go.** It caught 1 of 9 failures in round 1, because the
   failures were in packages that depend on the change. With the import graph it's 9 of 10, and
   the remaining miss was covered by a fallback.
3. **Binary-level suites dominate.** `test/cli` is about 85% of kubo's test time and is reachable
   from most packages. Realistic savings on kubo come from docs, version-only and narrow unit
   changes.
4. **Backfill diffs must match what `siftwise select` sends.** With head-vs-first-parent diffs, PRs
   looked smaller than they were. That inflated the savings (99.6%) and created false misses
   (`ondemandpin`).
5. **Co-change only helps after a first miss.** It caught `TestBackupBootstrapPeers` on the
   re-push, not the first time, so it can't be the safety net.
6. **Not measured:**
   - recall on a selective run that has failures (none occurred);
   - recall in the main replay (main had no failures in the window).

   The rdflib evaluation is meant to cover both.

### Reproducing

This needs a kubo clone, Go, a siftwise server with kubo backfilled, and `GITHUB_TOKEN` (for
`--replay-main`).

```sh
uv run python scripts/eval_kubo.py --failing --siftwise-toml scripts/eval/kubo.siftwise.toml \
    --kubo-checkout ../kubo --go go
uv run python scripts/eval_kubo.py --replay-main --siftwise-toml scripts/eval/kubo.siftwise.toml \
    --kubo-checkout ../kubo --go go
```

GitHub compares and import graphs are cached in `.eval-cache/`.

## RDFLib/rdflib

### Summary

- **Final: no misses in selective runs.** All 2,050 failures in the 39 failing PR runs were
  caught, 140 of 140 in the 17 runs that went selective, plus 1 of 1 in the main replay.
  Round 1 (path mapping only) caught 1 of 7 in selective runs.
- **What closed the gaps:** a static Python import graph from the CLI (including parent
  packages, conftests and module names in strings), whole-file runs for changed or new test
  files, doctest modules, and running changed modules under `--doctest-modules` when the repo
  imports source modules. See [Round 2](#round-2-python-import-graph-whole-test-files-doctests).
- **But rdflib saves almost nothing.** Its package `__init__.py` and plugin registry connect
  nearly every module to nearly every test file, so a selective run still runs about 99% of
  test time. Estimated time skipped across all runs fell from 10–19% (round 1, with misses)
  to 0.2–0.6%.
- **`/select` takes 0.3–1.4 s**, down from 79–124 s, after the history and collection-error
  fixes below.

### Data and method

- 190 CI runs (20 on main) from 2026-07-12 to 2026-10-07: 2,959 uploads (one per matrix leg),
  29.2M results, 40,145 distinct test IDs, about 18,000 of them known at any one time.
- Same method as for kubo, with two differences:
  - **Python import graph.** In round 1, selection used path mapping, co-change and recently
    failed, and a static import graph at each commit only explained misses. In round 2 the
    graph is computed at each target commit and passed to the selector, as `siftwise select`
    does.
  - **History is loaded once.** The eval reads the repo's raw results into memory once (about
    100 s) and rebuilds each target's history from that. PR mode took 7.7 minutes for 49
    targets, and the main replay 2.3 minutes.
- No `.siftwise.toml` (no declared dependencies or always-run patterns).

### Fixes made before these numbers

A first attempt showed that `/select` took 79–124 s on rdflib, and that every code change went
full. Three fixes came first:

| Problem | Fix |
|---|---|
| Building history scanned raw results on every call: 73 s for known tests (about 8M rows: 50 CI runs × 16 legs × about 9,900 results) and 36 s for co-change failures | Known tests are read from `test_stats.last_seen_run_id`, which the rollup keeps. Failures are read through a partial index on failed results (2.4 MB for 13,329 rows). `load_history` never reads passing raw results; a test checks this with `EXPLAIN ANALYZE`. |
| pytest collection errors (`pytest::rdflib.plugins.serializers.n3`, a module that failed to import) had no runner, and co-change selected them in almost every code change, forcing the full suite | They map to the module's file (`rdflib/plugins/serializers/n3.py`), which pytest runs whole. If no file can be derived, they're left out of the command instead of forcing the full suite. |
| The per-upload rollup sometimes chose a nested loop that re-read the upload's results for every result in the window (about 40 s per upload on 120 uploads × 150 tests) | The upload's test list is materialized once. |

`POST /select`, timed over HTTP on the same data:

| Change | Before | After | Result after |
|---|---|---|---|
| Source: `rdflib/plugins/sparql/parser.py` | 78.9 s, full (collection error) | 1.36 s (first call) | selective, 347 tests |
| Test: `test/test_sparql/test_agg_distinct.py` | 94.1 s, full (collection error) | 0.33 s | selective, 14 tests |
| Docs: `docs/index.md` | 123.7 s | 0.34 s | selective, 1 test |

After the fix, loading history takes 0.11–0.85 s and selecting about 0.1 s. Every history query
takes 0.05 s or less; the rest is Python building 18,000 known-test entries. The "before" times
were measured while an eval was loading the same database, so they are inflated.

### Round 1: PR mode (path mapping only)

| | 10 most recent PR runs | 39 PR runs with failures |
|---|---|---|
| Full / selective | 8 / 2 | 35 / 4 |
| Full: changed file maps to no known tests | 3 | 21 |
| Full: build or CI file changed | 4 | 14 |
| Full: selected test has no runner | 1 (a doctest, see findings) | 0 |
| Time skipped in selective runs (average) | 96.7% | 99.0% |
| **Estimated time skipped, all runs** | **19.3%** | **10.2%** |
| Failures caught | none to catch | 2,044 / 2,050 |
| Caught in selective runs | — | **1 / 7** |
| **Caught without fallbacks** (full runs) | — | **1,160 / 1,954**, plus 89 failures that only a fallback protected |

**Selective runs with failures**

| Run | Change | Selected | Failures | Missed, and the signal that would catch it |
|---|---|---|---|---|
| 7d6a498 `undef-enum` | `rdflib/plugins/shared/jsonld/context.py` | 381 | 1 | `test_sparql.test_service::test_service`: flaky / recurring. It also failed in an earlier unrelated run, and it calls a remote SPARQL service. |
| 1e924cc `fix/uri-control-character-validation` | `rdflib/term.py`, `test/test_literal/test_term.py` | 8 | 4 | All 4: Python imports. `test_aggregate_graphs`, `test_namespacemanager` and two `test_store_sparqlstore` tests import `rdflib/term.py`, directly or through `sparqlstore.py`. |
| 81c247a `history` | 13 files, mostly docs | 26 | 1 | none: caught |
| 9839265 `history` | the same change, re-pushed | 25 | 1 | `test_service::test_service_with_values`: Python imports (through `rdflib/store.py`); probably also flaky, like `test_service`. |

**Without fallbacks** (what the selector's own signals catch in the runs that went full):

| Signal that would catch the miss | Failures | Where most of them come from |
|---|---|---|
| Python imports | 685 | `upgrade-mypy` (343 failures, 41 files changed) and `fix/dataset-length-count` (311 failures from a 4-file change that selects 21 tests) |
| New test (not in history yet) | 106 | `complete-uv` 51, `owlrl` 21 + 13 + 1, `upgrade-mypy-v3` 12, `longturtle-default` 8 |
| Flaky / recurring | 1 | |
| Python imports, but no graph for that test | 1 | |
| None: looks unrelated to the change | 1 | |

### Round 1: main replay (`--replay-main`, 19 main commits replayed as PRs)

| | |
|---|---|
| Full / selective | 17 (89%) / 2 (11%) |
| Full: changed file maps to no known tests | 9 |
| Full: build or CI file changed | 7 |
| Full: selected test has no runner (a doctest) | 1 |
| Time skipped in selective runs (average) | 94.0% |
| **Estimated time skipped, all runs** | **9.9%** |
| Failures | 1 in 1 run, caught by the full-suite fallback. Without fallbacks, nothing maps: `rdflib/plugins/serializers/nt.py` has no test named after it. |

### Round 1 findings

Items 1–3 are addressed in round 2.


1. **Python needs an import graph, as Go did.** Path mapping by file name misses most of
   rdflib's dependencies. It forces the full suite for 33 of the 60 full runs here, and when it
   does select, it missed 5 of 7 failures. Imports are the signal for 685 of the 794
   counterfactual misses and 5 of the 6 real ones. Like `go list` for kubo, the CLI could
   compute the importers of each changed module, using `scripts/eval/pygraph.py` as a
   starting point. That should both catch these misses and turn many full runs selective.
2. **Tests new in the PR are never run by a selective Python run.** Commands name only known
   pytest node IDs, so a test added in the change isn't run, even when its file changed (106
   counterfactual misses). Running changed test files whole, as changed Go packages already
   are, would cover the ones in changed files.
3. **Doctests have no runner.** IDs such as `rdflib.container::rdflib.container.Container`
   (classname = a source module) can't be mapped to a command, so selecting one forces the full
   suite (1 recent PR, 1 main commit). They could map to `pytest rdflib/container.py`, like
   collection errors now do.
4. **Flaky tests are now measurable.** `test_service` failed in a selective run and in an
   earlier unrelated run, and it depends on a remote service. The week-4 flaky signal would
   label it rather than count it as a miss.
5. **Matrix repos multiply history size.** About 16 uploads per CI run made every raw-results
   query 16× bigger than on kubo. `/select` now avoids raw results entirely. The batch rollup
   still reads the whole 90-day window: about 16 minutes and up to 15 GB of Postgres temp
   files for 29M results.

### Round 2: Python import graph, whole test files, doctests

**Changes**

- **Python import graph.** `siftwise select` builds a static import graph of the checkout with
  `ast` (`siftwise.cli.pygraph`), like `go list` for Go. It includes parent packages'
  `__init__.py` and the `conftest.py` files pytest loads. It sends every test file that imports
  a changed module, directly or transitively, as `affected_files`. The selector runs those
  files whole, with the reason `imports changed module X`, and a changed module imported by a
  known test file no longer forces the full suite. If the graph can't be built, nothing is
  sent and the fallbacks apply.
- **Module names in strings count as imports.** rdflib registers its parsers and serializers
  by module path (`register("nt", Serializer, "rdflib.plugins.serializers.nt", ...)`), so the
  first version of the graph didn't connect them to the tests that use them. A string literal
  that is exactly the dotted name of a module in the repo is now an import edge.
- **Changed or added test files run whole** (`pytest test/test_x.py`), so tests new in them
  run. The CLI also sends deleted paths, so a deleted test file is never named in a command.
- **Doctests** (`rdflib.container::rdflib.container.Container`) run as `pytest
  --doctest-modules rdflib/container.py`. If no file can be derived, they're left out of the
  command instead of forcing the full suite.
- **Changed modules run under `--doctest-modules`** when history shows the repo's pytest
  imports source modules (doctests, or collection errors in non-test modules). This catches a
  new module that fails to import. pytest exits 5 for a module with no doctests, so that
  command accepts exit 5; import errors (exit 2) and failing doctests still fail.

**Before and after**

| | Round 1 (path mapping) | Graph, first version | + module names in strings | **Final** |
|---|---|---|---|---|
| **39 PR runs with failures** | | | | |
| Full / selective | 35 / 4 | 24 / 15 | 22 / 17 | **22 / 17** |
| Failures caught | 2,044 / 2,050 | 1,991 / 2,050 | 2,037 / 2,050 | **2,050 / 2,050** |
| **Caught in selective runs** | 1 / 7 | 58 / 117 | 127 / 140 | **140 / 140** |
| Caught without fallbacks (full runs) | 1,160 / 1,954 | 1,873 / 1,931 | 1,864 / 1,908 | **1,880 / 1,908** |
| Estimated time skipped, all runs | 10.2% | 5.0% | 0.2% | **0.2%** |
| **10 most recent PR runs** | | | | |
| Full / selective | 8 / 2 | 6 / 4 | 5 / 5 | **5 / 5** |
| Estimated time skipped, all runs | 19.3% | 0.3% | 0.4% | **0.4%** |
| **Main replay (19 commits)** | | | | |
| Full / selective | 17 / 2 | 8 / 11 | 7 / 12 | **7 / 12** |
| Failures caught (in selective runs) | 1 / 1 (by the fallback) | 1 / 1 (1 / 1) | 1 / 1 (1 / 1) | **1 / 1 (1 / 1)** |
| Estimated time skipped, all runs | 9.9% | 25.8% | 0.6% | **0.6%** |

What each step fixed:

- **Round 1 → first graph:** catches the 5 import misses after `rdflib/term.py` and
  `rdflib/store.py` changes.
- **First graph → module names in strings:** catches the 58 failures of `string-n-serialization`
  (round-trip, serializer and W3C N-Triples/N-Quads tests after an `nt`/`nquads` serializer
  change), which reach the serializers only through `rdflib/plugin.py`. It also selects
  `test_service`, the recurring network test, after a JSON-LD context change, which it reaches
  through the same registry.
- **→ final:** catches the 13 failures of an `owlrl` run that added the new package
  `rdflib/plugins/inference/`, whose modules failed to import (`pytest::rdflib.plugins.inference.closure`, ...).

**Remaining full-suite reasons** (8 failing PR runs, 1 recent PR run): files that no known test
reaches. These are `.pre-commit-config.yaml`, `examples/datasets.py`, and new modules that
nothing imports yet (`rdflib/plugins/serializers/longturtle.py`, a new `rdflib/inference/`
package). Without fallbacks, 28 failures would be missed: all are collection errors of modules the
run didn't change, in runs that went full anyway.

**Findings**

1. **0 misses in selective runs, but almost no savings on rdflib.** `rdflib/__init__.py`
   imports most of the library, and `rdflib/plugin.py` names every plugin. So a change to
   almost any module reaches 325 of 326 test files: selective runs skip about 1% of test
   time. That's correct for this codebase: statically, nearly every test can reach nearly
   every module. The 25.8% of the first graph version came from missing the plugin edges, and
   it missed 58 failures.
2. **File-level selection can't separate rdflib's tests.** Savings here would need a finer
   signal, such as per-test coverage (which test executes which module), not a better import
   graph.
3. **The safety mechanisms are general.** Import graph with parent packages and conftests,
   module-name strings, whole test files, doctest modules and deleted files apply to any
   Python repo. A repo with less central `__init__` imports should see real savings; that
   needs a second Python repo to measure.
4. **Costs.** Building the graph takes about 5 s for rdflib's 538 files, and finding affected
   files is instant. Eval runs: PR mode 8 minutes, main replay 2 minutes.

### Reproducing

This needs an rdflib clone, a siftwise server with rdflib backfilled and rolled up (`POST
/repos/RDFLib/rdflib/rollup`), and `GITHUB_TOKEN` for `--replay-main`.

```sh
uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --failing --checkout ../rdflib
uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --replay-main --checkout ../rdflib
```
