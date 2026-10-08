import shlex
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from sieve.core.ingest import create_run
from sieve.core.junit import ParsedTestResult, Status
from sieve.core.schemas import RunMetadata
from sieve.core.selector import (
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Runner,
    Selection,
    SelectorConfig,
    is_build_file,
    load_history,
    select_for_repo,
    select_tests,
)

KUBO = "github.com/ipfs/kubo"
CLI = f"{KUBO}/test/cli"
COREUNIX = f"{KUBO}/core/coreunix"


def history(*tests: str | tuple[str, str] | KnownTest, **kwargs: object) -> RepoHistory:
    """Known tests given as test_id, (test_id, file_path) or KnownTest."""
    known = {}
    for test in tests:
        if isinstance(test, str):
            test = KnownTest(test)
        elif isinstance(test, tuple):
            test = KnownTest(*test)
        known[test.test_id] = test
    return RepoHistory(tests=known, **kwargs)  # type: ignore[arg-type]


def select(
    hist: RepoHistory | None,
    *changed: str,
    known: bool = True,
    **config: object,
) -> Selection:
    return select_tests(hist, changed, known, SelectorConfig(**config))  # type: ignore[arg-type]


def ids(selection: Selection) -> list[str]:
    return [t.test_id for t in selection.tests]


def reasons(selection: Selection) -> dict[str, str]:
    return {t.test_id: t.reason for t in selection.tests}


PY = history(
    "tests.test_cart::test_total",
    "tests.test_cart.TestDiscount::test_applies",
    "tests.unit.test_cart::test_unit",
    "tests.test_cartography::test_maps",
    "tests.pricing_test::test_price",
    "tests.test_io::test_read",
)


# --- full-suite fallbacks -----------------------------------------------------------------


def test_unknown_changed_files_run_the_full_suite() -> None:
    selection = select(PY, "src/shop/cart.py", known=False)

    assert selection.mode is Mode.FULL
    assert selection.reason == "changed files are unknown for this change"
    assert selection.tests == ()
    assert (selection.selected_count, selection.total_known) == (6, 6)
    assert selection.commands == ("pytest",)


@pytest.mark.parametrize("changed", [(), ("",), ("  ",)], ids=["empty", "blank", "whitespace"])
def test_empty_changed_files_run_the_full_suite(changed: tuple[str, ...]) -> None:
    selection = select(PY, *changed)
    assert selection.mode is Mode.FULL
    assert selection.reason == "no changed files were given"


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "requirements-dev.txt",
        "tests/conftest.py",
        "package.json",
        "web/package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "tsconfig.build.json",
        "jest.config.ts",
        "go.mod",
        "go.sum",
        "Dockerfile",
        "docker/Dockerfile.dev",
        "docker-compose.yml",
        ".github/workflows/ci.yml",
        "Makefile",
    ],
)
def test_build_or_config_change_runs_the_full_suite(path: str) -> None:
    selection = select(PY, "src/shop/cart.py", path)

    assert selection.mode is Mode.FULL
    assert selection.reason == f"build/config file changed: {path}"


def test_build_reason_lists_several_files() -> None:
    selection = select(PY, "go.mod", "go.sum", "package.json", "uv.lock", "Dockerfile")
    assert selection.reason == ("build/config file changed: Dockerfile, go.mod, go.sum (+2 more)")


@pytest.mark.parametrize("path", ["src/cart.py", "README.md", ".github/CODEOWNERS"])
def test_not_build_files(path: str) -> None:
    assert not is_build_file(path)


@pytest.mark.parametrize("hist", [None, history()], ids=["unknown-repo", "no-tests"])
def test_no_history_runs_the_full_suite(hist: RepoHistory | None) -> None:
    selection = select(hist, "src/shop/cart.py")

    assert selection.mode is Mode.FULL
    assert selection.reason == "no test history for this repo"
    assert selection.commands == ()  # no idea which runner the repo uses


@pytest.mark.parametrize(
    "path",
    [
        "src/shop/utils.py",  # no test_utils.py
        "src/shop/__init__.py",
        "proto/shop.proto",  # unknown language
        "tests/fixtures/order.json",  # test data
        "tests/test_new.py",  # a new test file has no known tests yet
    ],
)
def test_source_file_with_no_tests_runs_the_full_suite(path: str) -> None:
    selection = select(PY, "src/shop/cart.py", path)

    assert selection.mode is Mode.FULL
    assert selection.reason == f"changed file maps to no known tests: {path}"


def test_selected_test_without_a_runner_runs_the_full_suite() -> None:
    # A Java test can be selected (here: it failed on main) but there's no command for it.
    hist = history(
        "tests.test_cart::test_total",
        "com.acme.CartTest::testTotal",
        recent_main_failures={"com.acme.CartTest::testTotal": (7, "c" * 40)},
    )
    selection = select(hist, "src/shop/cart.py")

    assert selection.mode is Mode.FULL
    assert (
        selection.reason
        == "no known test runner for selected test(s): com.acme.CartTest::testTotal"
    )


# --- path mapping: Python -----------------------------------------------------------------


def test_python_source_selects_test_modules_by_name() -> None:
    selection = select(PY, "src/shop/cart.py")

    assert selection.mode is Mode.SELECTIVE
    assert ids(selection) == [
        "tests.test_cart.TestDiscount::test_applies",
        "tests.test_cart::test_total",
        "tests.unit.test_cart::test_unit",  # any directory
    ]
    assert reasons(selection)["tests.unit.test_cart::test_unit"] == (
        "tests/unit/test_cart.py tests changed src/shop/cart.py"
    )
    assert (selection.selected_count, selection.total_known) == (3, 6)
    assert selection.reason == "3 of 6 known tests selected"


def test_python_foo_test_naming() -> None:
    selection = select(PY, "src/shop/pricing.py")
    assert ids(selection) == ["tests.pricing_test::test_price"]


def test_changed_python_test_file_selects_its_own_tests_only() -> None:
    selection = select(PY, "tests/test_cart.py")

    assert ids(selection) == [
        "tests.test_cart.TestDiscount::test_applies",
        "tests.test_cart::test_total",
    ]
    assert set(reasons(selection).values()) == {"test file changed: tests/test_cart.py"}


def test_python_file_path_attribute_is_used_when_present() -> None:
    # xunit1 output carries the real file; it wins over the classname-derived path.
    hist = history(("pkg.test_cart::test_total", "src/pkg/tests/test_cart.py"))
    selection = select(hist, "src/pkg/tests/test_cart.py")

    assert ids(selection) == ["pkg.test_cart::test_total"]
    assert selection.commands == ("pytest src/pkg/tests/test_cart.py::test_total",)


def test_python_module_with_custom_name_uses_file_path() -> None:
    # python_files = "check_*.py": the module isn't recognisable from the classname alone.
    hist = history(("tests.check_cart.CartChecks::test_total", "tests/check_cart.py"))
    selection = select(hist, "tests/check_cart.py")

    assert selection.commands == ("pytest tests/check_cart.py::CartChecks::test_total",)


# --- path mapping: Go ---------------------------------------------------------------------

GO = history(
    f"{CLI}::TestAdd",
    f"{CLI}::TestAdd/produced_cid_version:_implicit_default_(CIDv0)",
    f"{CLI}::TestPins/test_pinning/test_pins_with_args={{runDaemon:true}}",
    f"{COREUNIX}::TestAdd",
    f"{COREUNIX}::TestAdd/ipfs_add_--to-files",
    f"{KUBO}::TestCommands",
)


def test_go_file_selects_every_test_in_its_package() -> None:
    selection = select(GO, "test/cli/add.go")

    assert ids(selection) == [
        f"{CLI}::TestAdd",
        f"{CLI}::TestAdd/produced_cid_version:_implicit_default_(CIDv0)",
        f"{CLI}::TestPins/test_pinning/test_pins_with_args={{runDaemon:true}}",
    ]
    assert reasons(selection)[f"{CLI}::TestAdd"] == (
        f"in Go package {CLI}, which has changed test/cli/add.go"
    )


def test_go_test_file_selects_its_package() -> None:
    # Same-named TestAdd in another package is not selected.
    selection = select(GO, "core/coreunix/add_test.go")
    assert ids(selection) == [f"{COREUNIX}::TestAdd", f"{COREUNIX}::TestAdd/ipfs_add_--to-files"]


def test_go_root_package_needs_go_module() -> None:
    assert select(GO, "commands.go").reason == "changed file maps to no known tests: commands.go"

    selection = select(GO, "commands.go", go_module=KUBO)
    assert ids(selection) == [f"{KUBO}::TestCommands"]
    assert selection.commands == ("go test . -run '^(TestCommands)$'",)


def test_go_module_makes_package_matching_exact() -> None:
    # Without go_module, "cli" suffix-matches .../test/cli (safe over-selection).
    hist = history(f"{CLI}::TestAdd", f"{KUBO}/cli::TestMain")
    assert len(select(hist, "cli/main.go").tests) == 2
    assert ids(select(hist, "cli/main.go", go_module=KUBO)) == [f"{KUBO}/cli::TestMain"]


# --- path mapping: JS/TS ------------------------------------------------------------------

JS = history(
    ("cart totals::cart totals adds", "src/cart.test.ts"),
    ("cart ui::cart ui renders", "src/cart.spec.tsx"),
    ("cart legacy::cart legacy works", "src/__tests__/cart.js"),
    ("cartography::maps", "src/cartography.test.ts"),
    ("checkout::pays", "src/checkout.test.ts"),
)


def test_js_source_selects_test_and_spec_files() -> None:
    selection = select(JS, "src/cart.ts")

    assert ids(selection) == [
        "cart legacy::cart legacy works",
        "cart totals::cart totals adds",
        "cart ui::cart ui renders",
    ]
    assert reasons(selection)["cart ui::cart ui renders"] == (
        "src/cart.spec.tsx tests changed src/cart.ts"
    )


def test_changed_js_test_file_selects_its_own_tests() -> None:
    selection = select(JS, "src/checkout.test.ts")
    assert ids(selection) == ["checkout::pays"]


def test_js_test_without_file_path_cannot_be_mapped() -> None:
    # Default jest-junit output has no file attribute: nothing maps, so run everything.
    hist = history("cart totals::cart totals adds")
    assert select(hist, "src/cart.ts").mode is Mode.FULL


# --- other signals ------------------------------------------------------------------------


def test_documentation_changes_are_ignored() -> None:
    selection = select(PY, "README.md", "docs/guide.txt", "LICENSE", "img/logo.png")

    assert selection.mode is Mode.SELECTIVE
    assert selection.tests == ()
    assert selection.commands == ()
    assert selection.command == ""
    assert selection.reason == "0 of 6 known tests selected"


def test_co_change_selects_tests_that_failed_with_these_files() -> None:
    hist = history(
        *PY.tests.values(),
        failed_runs=(
            FailedRun(9, "9" * 40, frozenset({"src/shop/cart.py", "src/db.py"}),
                      frozenset({"tests.test_io::test_read"})),
            FailedRun(8, "8" * 40, frozenset({"src/other.py"}),
                      frozenset({"tests.test_cartography::test_maps"})),
            FailedRun(7, "7" * 40, frozenset({"src/shop/cart.py"}),
                      frozenset({"tests.test_io::test_read", "tests.deleted::test_gone"})),
        ),
    )  # fmt: skip
    selection = select(hist, "src/shop/cart.py")

    assert "tests.test_io::test_read" in ids(selection)
    assert "tests.test_cartography::test_maps" not in ids(selection)  # no overlap
    assert "tests.deleted::test_gone" not in ids(selection)  # no longer a known test
    # One reason, from the most recent overlapping run.
    assert reasons(selection)["tests.test_io::test_read"] == (
        "co-change: failed in run 9 (9999999), which also changed src/shop/cart.py"
    )


def test_co_change_ignores_overlap_on_documentation() -> None:
    hist = history(
        *PY.tests.values(),
        failed_runs=(
            FailedRun(
                9, "9" * 40, frozenset({"README.md"}), frozenset({"tests.test_io::test_read"})
            ),
        ),
    )
    selection = select(hist, "src/shop/cart.py", "README.md")
    assert "tests.test_io::test_read" not in ids(selection)


def test_recently_failed_and_broken_on_main_are_selected() -> None:
    hist = history(
        *PY.tests.values(),
        broken_on_main={"tests.test_io::test_read": "b" * 40},
        recent_main_failures={
            "tests.test_io::test_read": (12, "c" * 40),
            "tests.test_cartography::test_maps": (11, "d" * 40),
        },
    )
    selection = select(hist, "README.md")

    assert reasons(selection) == {
        "tests.test_cartography::test_maps": "failed on main in run 11 (ddddddd)",
        "tests.test_io::test_read": (
            "broken on main since bbbbbbb; failed on main in run 12 (ccccccc)"
        ),
    }


def test_always_run_globs_match_test_ids_and_file_paths() -> None:
    hist = history(
        *PY.tests.values(),
        ("smoke::boots", "e2e/smoke.test.ts"),
    )
    selection = select(hist, "README.md", always_run=("tests.test_io::*", "e2e/*"))

    assert reasons(selection) == {
        "smoke::boots": "always-run pattern 'e2e/*'",
        "tests.test_io::test_read": "always-run pattern 'tests.test_io::*'",
    }


def test_signals_combine_into_one_entry_with_all_reasons() -> None:
    hist = history(
        *PY.tests.values(), recent_main_failures={"tests.test_cart::test_total": (5, "e" * 40)}
    )
    selection = select(hist, "src/shop/cart.py", always_run=("tests.test_cart::*",))

    assert reasons(selection)["tests.test_cart::test_total"] == (
        "tests/test_cart.py tests changed src/shop/cart.py; "
        "failed on main in run 5 (eeeeeee); always-run pattern 'tests.test_cart::*'"
    )


# --- commands -----------------------------------------------------------------------------


def test_pytest_command_uses_node_ids_and_quotes_params() -> None:
    hist = history(
        "tests.test_cart::test_total",
        "tests.test_cart.TestDiscount.TestNested::test_applies",
        "tests.test_cart::test_param[a b-1]",
    )
    selection = select(hist, "src/cart.py")

    assert selection.commands == (
        "pytest tests/test_cart.py::TestDiscount::TestNested::test_applies "
        "'tests/test_cart.py::test_param[a b-1]' tests/test_cart.py::test_total",
    )
    assert shlex.split(selection.command)[2] == "tests/test_cart.py::test_param[a b-1]"


def test_go_commands_group_by_package_and_run_top_level_tests() -> None:
    selection = select(GO, "test/cli/add.go", "core/coreunix/add.go", go_module=KUBO)

    assert selection.commands == (
        "go test ./core/coreunix -run '^(TestAdd)$'",
        "go test ./test/cli -run '^(TestAdd|TestPins)$'",
    )
    assert selection.command == " && ".join(selection.commands)


def test_go_commands_use_import_paths_without_go_module() -> None:
    selection = select(GO, "test/cli/add.go")
    assert selection.commands == (f"go test {CLI} -run '^(TestAdd|TestPins)$'",)


def test_jest_command_lists_test_files() -> None:
    selection = select(JS, "src/cart.ts")
    assert selection.commands == ("jest src/__tests__/cart.js src/cart.spec.tsx src/cart.test.ts",)


def test_mixed_runners_get_one_command_each() -> None:
    hist = history(*PY.tests.values(), *GO.tests.values(), *JS.tests.values())
    selection = select(hist, "src/shop/cart.py", "test/cli/add.go", "src/checkout.ts")

    assert [shlex.split(c)[0] for c in selection.commands] == ["pytest", "go", "jest"]
    assert {t.runner for t in selection.tests} == {Runner.PYTEST, Runner.GO, Runner.JEST}


def test_full_suite_commands_cover_every_runner_in_history() -> None:
    hist = history(*PY.tests.values(), *GO.tests.values(), *JS.tests.values())
    assert select(hist, "go.mod").commands == ("pytest", "go test ./...", "jest")


# --- loading history from the database ----------------------------------------------------

REPO = "acme/shop"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def ingest(
    session: Session,
    n: int,
    tests: dict[str, Status],
    *,
    is_main: bool = True,
    changed: tuple[str, ...] = (),
    changed_known: bool = True,
) -> int:
    results = [
        ParsedTestResult(
            test_id=test_id,
            classname=test_id.split("::")[0],
            name=test_id.split("::")[1],
            file_path=None,
            status=status,
            duration_ms=1,
            attempt=1,
        )
        for test_id, status in tests.items()
    ]
    meta = RunMetadata(
        repo=REPO,
        commit_sha=f"{n:040x}",
        branch="main" if is_main else "feature",
        is_main=is_main,
        started_at=T0 + timedelta(hours=n),
        changed_files=list(changed),
        changed_files_known=changed_known,
    )
    run, _ = create_run(session, meta, results)
    return run.id


P, F = Status.PASSED, Status.FAILED


def test_load_history_unknown_repo(db_session: Session) -> None:
    assert load_history(db_session, "nobody/nothing") is None
    assert select_for_repo(db_session, "nobody/nothing", ["a.py"], True).reason == (
        "no test history for this repo"
    )


def test_load_history_from_runs(db_session: Session) -> None:
    run1 = ingest(db_session, 1, {"tests.test_a::test_x": F, "tests.test_b::test_y": P},
                  is_main=False, changed=("src/a.py",))  # fmt: skip
    run2 = ingest(db_session, 2, {"tests.test_a::test_x": P, "tests.test_b::test_y": F})
    ingest(db_session, 3, {"tests.test_a::test_x": F}, changed=("src/c.py",), changed_known=False)

    hist = load_history(db_session, REPO, SelectorConfig(recent_main_runs=1))
    assert hist is not None

    assert set(hist.tests) == {"tests.test_a::test_x", "tests.test_b::test_y"}
    # recent_main_runs=1: only run 3 counts; test_b's main failure (run 2) is older.
    assert set(hist.recent_main_failures) == {"tests.test_a::test_x"}
    # Broken on main comes from test_stats: each test's latest main outcome is a failure.
    assert hist.broken_on_main == {
        "tests.test_a::test_x": f"{3:040x}",
        "tests.test_b::test_y": f"{2:040x}",
    }
    # Run 3's diff is unknown, so it is not co-change evidence; runs 1 and 2 are (newest first).
    assert [(r.run_id, r.changed_files, r.failed_tests) for r in hist.failed_runs] == [
        (run2, frozenset(), frozenset({"tests.test_b::test_y"})),
        (run1, frozenset({"src/a.py"}), frozenset({"tests.test_a::test_x"})),
    ]


def test_known_tests_are_limited_to_recent_runs(db_session: Session) -> None:
    ingest(db_session, 1, {"tests.test_old::test_gone": P})
    ingest(db_session, 2, {"tests.test_a::test_x": P})

    hist = load_history(db_session, REPO, SelectorConfig(known_test_runs=1))
    assert hist is not None
    assert set(hist.tests) == {"tests.test_a::test_x"}


def test_select_for_repo_end_to_end(db_session: Session) -> None:
    run1 = ingest(db_session, 1, {"tests.test_cart::test_total": F, "tests.test_io::test_read": F},
                  is_main=False, changed=("src/cart.py",))  # fmt: skip
    ingest(db_session, 2, {"tests.test_cart::test_total": P, "tests.test_io::test_read": P,
                           "tests.test_db::test_q": P})  # fmt: skip

    selection = select_for_repo(db_session, REPO, ["src/cart.py"], True)

    assert selection.mode is Mode.SELECTIVE
    co_change = f"co-change: failed in run {run1} (0000000), which also changed src/cart.py"
    assert reasons(selection) == {
        "tests.test_cart::test_total": f"tests/test_cart.py tests changed src/cart.py; {co_change}",
        "tests.test_io::test_read": co_change,
    }
    assert (selection.selected_count, selection.total_known) == (2, 3)
    assert selection.command == (
        "pytest tests/test_cart.py::test_total tests/test_io.py::test_read"
    )
