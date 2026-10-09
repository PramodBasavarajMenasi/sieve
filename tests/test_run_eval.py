"""Core logic of scripts/eval/run_eval.py: what would run, why misses happen, no peeking."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from scripts.eval import run_eval as ev
from sieve.cli.pygraph import PyGraph
from sieve.core.ingest import create_run
from sieve.core.junit import ParsedTestResult, Status
from sieve.core.schemas import RunMetadata
from sieve.core.selector import (
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Runner,
    SelectedTest,
    Selection,
    SelectorConfig,
    load_history,
)

PKG = "github.com/acme/shop/cart"


def selection(
    *tests: tuple[str, Runner], whole: tuple[str, ...] = (), full: bool = False
) -> Selection:
    return Selection(
        mode=Mode.FULL if full else Mode.SELECTIVE,
        reason="",
        tests=tuple(SelectedTest(t, ("r",), runner, None) for t, runner in tests),
        selected_count=len(tests),
        total_known=100,
        commands=(),
        go_packages_run_whole=whole,
    )


# --- would_run ----------------------------------------------------------------------------


def test_full_mode_runs_everything() -> None:
    assert ev.would_run(selection(full=True), "anything::at_all")


def test_exact_test_runs() -> None:
    sel = selection(("tests.test_a::test_x", Runner.PYTEST))
    assert ev.would_run(sel, "tests.test_a::test_x")
    assert not ev.would_run(sel, "tests.test_a::test_y")


def test_whole_go_package_runs_every_test_in_it_including_new_ones() -> None:
    sel = selection((f"{PKG}::TestOld", Runner.GO), whole=(PKG,))
    assert ev.would_run(sel, f"{PKG}::TestBrandNew/sub")
    assert not ev.would_run(sel, "github.com/acme/shop/pay::TestOld")


def test_whole_python_file_runs_every_test_in_it_including_new_ones() -> None:
    sel = Selection(
        mode=Mode.SELECTIVE, reason="", tests=(), selected_count=0, total_known=10, commands=(),
        python_files_run_whole=("tests/test_cart.py", "rdflib/container.py"),
    )  # fmt: skip
    assert ev.would_run(sel, "tests.test_cart.TestNew::test_brand_new")
    assert ev.would_run(sel, "rdflib.container::rdflib.container.Seq")  # doctest module
    assert not ev.would_run(sel, "tests.test_other::test_x")


def test_go_run_filter_covers_subtests_of_selected_top_level_tests() -> None:
    sel = selection((f"{PKG}::TestTotal/discount", Runner.GO))
    assert ev.would_run(sel, f"{PKG}::TestTotal/other_subtest")  # -run '^(TestTotal)$'
    assert not ev.would_run(sel, f"{PKG}::TestOther")


# --- explaining misses --------------------------------------------------------------------

SHA = "a" * 40


def history(*tests: str, failed_runs: tuple[FailedRun, ...] = ()) -> RepoHistory:
    return RepoHistory(tests={t: KnownTest(t) for t in tests}, failed_runs=failed_runs)


@pytest.fixture
def graphs(tmp_path: Path) -> ev.GraphProvider:
    """A provider whose cache holds a Python graph at SHA (no checkout needed)."""
    graph = PyGraph(
        {
            "tests/test_cart.py": frozenset({"shop/cart.py"}),
            "shop/cart.py": frozenset({"shop/money.py"}),
            "shop/money.py": frozenset(),
            "tests/test_other.py": frozenset(),
        }
    )
    (tmp_path / f"pygraph-{SHA}.json").write_text(json.dumps(graph.to_json()))
    return ev.GraphProvider(None, "go", tmp_path)


def explainer(
    hist: RepoHistory, changed: list[str], graphs: ev.GraphProvider, go: bool = False
) -> ev.Explainer:
    return ev.Explainer(hist, changed, None, go, None, graphs, SHA)


def test_new_test_cannot_be_named_by_any_signal(graphs: ev.GraphProvider) -> None:
    missed = explainer(history(), ["shop/money.py"], graphs)("tests.test_cart::test_new")
    assert (missed.known_before, missed.signal) == (False, "new test")


def test_python_import_chain_explains_a_miss(graphs: ev.GraphProvider) -> None:
    missed = explainer(history("tests.test_cart::test_total"), ["shop/money.py"], graphs)(
        "tests.test_cart::test_total"
    )
    assert missed.signal == "python imports"
    assert missed.why == (
        "imports a changed module: tests/test_cart.py -> shop/cart.py -> shop/money.py"
    )


def test_recurring_failure_is_called_flaky(graphs: ev.GraphProvider) -> None:
    earlier = (FailedRun(7, "b" * 40, frozenset({"x.py"}), frozenset({"tests.test_other::t"})),)
    missed = explainer(history("tests.test_other::t", failed_runs=earlier), ["shop/money.py"],
                       graphs)("tests.test_other::t")  # fmt: skip
    assert missed.signal == "flaky / recurring"
    assert "1 earlier run(s) (latest run 7)" in missed.why


def test_unrelated_failure(graphs: ev.GraphProvider) -> None:
    hist = history("tests.test_other::t")
    no_python = explainer(hist, ["docs/index.md"], graphs)("tests.test_other::t")
    not_imported = explainer(hist, ["shop/money.py"], graphs)("tests.test_other::t")
    assert no_python.signal == not_imported.signal == "none (unrelated)"
    assert "doesn't import any changed module" in not_imported.why


def test_missing_python_graph(tmp_path: Path) -> None:
    graphs = ev.GraphProvider(None, "go", tmp_path)  # empty cache, no checkout
    missed = explainer(history("tests.test_cart::t"), ["shop/money.py"], graphs)(
        "tests.test_cart::t"
    )
    assert missed.signal == "python imports (graph unavailable)"


def test_go_miss_outside_the_import_graph_needs_a_declared_dependency(
    graphs: ev.GraphProvider,
) -> None:
    from sieve.cli.gograph import GoGraph

    go_graph = GoGraph("github.com/acme/shop", {}, {})
    test_id = "github.com/acme/shop/e2e::TestCheckout"
    missed = ev.Explainer(history(test_id), ["cart/cart.go"], "github.com/acme/shop", True,
                          go_graph, graphs, SHA)(test_id)  # fmt: skip
    assert missed.signal == "declared dependency"
    assert "./e2e has no changed files" in missed.why


# --- savings ------------------------------------------------------------------------------


def test_skipped_percentages() -> None:
    hist = history("t::a", "t::b", "t::c", "t::d")
    durations = {"t::a": 100.0, "t::b": 100.0, "t::c": 600.0, "t::d": 200.0}
    sel = selection(("t::a", Runner.PYTEST))

    tests_pct, time_pct = ev.skipped_pcts(sel, hist, durations)

    assert tests_pct == pytest.approx(75.0)  # 3 of 4 tests skipped
    assert time_pct == pytest.approx(90.0)  # 900 of 1000 ms skipped
    assert ev.skipped_pcts(selection(full=True), hist, durations) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("reason", "signal"),
    [
        ("imports changed package x", "go imports"),
        ("declared dependency: test/cli/** on a.go", "declared"),
        ("co-change: failed in run 3", "co-change"),
        ("failed on main in run 4 (abc)", "recently failed"),
        ("broken on main since abc", "recently failed"),
        ("always-run pattern 'x'", "always-run"),
        ("tests/test_a.py tests changed a.py", "path mapping"),
    ],
)
def test_signal_labels(reason: str, signal: str) -> None:
    assert ev._signal(reason) == signal


# --- targets and history from the database (no peeking) -----------------------------------

REPO = "acme/shop"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def ingest(session: Session, ci: int, hour: int, tests: dict[str, Status], *, variant: str | None,
           is_main: bool = False) -> int:  # fmt: skip
    results = [
        ParsedTestResult(t, t.split("::")[0], t.split("::")[1], None, s, 1, 1)
        for t, s in tests.items()
    ]
    meta = RunMetadata(
        repo=REPO, commit_sha=f"{ci:040x}", branch="main" if is_main else "feature",
        is_main=is_main, ci_run_id=str(ci), variant=variant,
        started_at=T0 + timedelta(hours=hour), changed_files=["src/a.py"],
    )  # fmt: skip
    run, _ = create_run(session, meta, results)
    return run.id


P, F = Status.PASSED, Status.FAILED


def test_targets_group_variants_and_union_their_failures(db_session: Session) -> None:
    linux = ingest(db_session, 7, 1, {"t::a": P, "t::b": F}, variant="linux")
    macos = ingest(db_session, 7, 1, {"t::a": F, "t::b": P}, variant="macos")
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()

    [target] = ev.pr_targets(db_session, repo_id, 10, failing=False)

    assert target.run_ids == (linux, macos)
    assert target.run_id == linux
    assert target.changed_files == ("src/a.py",)
    assert ev.actual_failures(db_session, list(target.run_ids)) == ["t::a", "t::b"]


def test_history_before_excludes_later_runs_and_sibling_variants(db_session: Session) -> None:
    ingest(db_session, 1, 1, {"t::early": P}, variant="linux", is_main=True)
    ingest(db_session, 2, 2, {"t::target_linux": F}, variant="linux")
    ingest(db_session, 2, 2, {"t::target_macos": F}, variant="macos")
    ingest(db_session, 3, 3, {"t::later": P}, variant="linux", is_main=True)
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    target = next(t for t in ev.pr_targets(db_session, repo_id, 10, False) if t.ci_run_id == "2")

    hist = ev.HistoryIndex(db_session, repo_id).history_before(target, SelectorConfig())

    # Only CI run 1 happened before CI run 2: neither its own variants nor run 3 leak in.
    assert set(hist.tests) == {"t::early"}
    assert hist.failed_runs == ()


S = Status.SKIPPED


def test_history_index_matches_load_history_after_the_last_run(db_session: Session) -> None:
    # Matrix CI runs on main and branches; a test broken on main on one variant only, a test
    # that recovers, one only skipped lately, and one that stopped appearing.
    ingest(db_session, 1, 1, {"t::a": P, "t::b": F, "t::gone": P}, variant="linux", is_main=True)
    ingest(db_session, 1, 1, {"t::a": P, "t::b": P, "t::gone": P}, variant="macos", is_main=True)
    ingest(db_session, 2, 2, {"t::a": F, "t::b": F, "t::c": P}, variant="linux")
    ingest(db_session, 3, 3, {"t::a": P, "t::b": F, "t::c": S}, variant="linux", is_main=True)
    ingest(db_session, 3, 3, {"t::a": F, "t::b": P, "t::c": S}, variant="macos", is_main=True)
    ingest(db_session, 4, 4, {"t::a": P, "t::b": F, "t::c": P}, variant="linux", is_main=True)
    repo_id = db_session.execute(
        text("SELECT id FROM repos WHERE name = :n"), {"n": REPO}
    ).scalar_one()
    config = SelectorConfig(known_test_runs=3, recent_main_runs=2)
    after_everything = ev.TargetRun(10**9, None, "f" * 40, "x", T0 + timedelta(days=1))

    from_index = ev.HistoryIndex(db_session, repo_id).history_before(after_everything, config)
    from_db = load_history(db_session, REPO, config)

    assert from_db is not None
    assert set(from_index.tests) == set(from_db.tests) == {"t::a", "t::b", "t::c"}
    # b: failing on linux main since commit 1; a: its latest macos main result failed (3).
    assert (
        from_index.broken_on_main
        == from_db.broken_on_main
        == {
            "t::a": f"{3:040x}",
            "t::b": f"{1:040x}",
        }
    )
    assert from_index.recent_main_failures == from_db.recent_main_failures
    assert set(from_index.recent_main_failures) == {"t::a", "t::b"}
    assert from_index.failed_runs == tuple(from_db.failed_runs)
