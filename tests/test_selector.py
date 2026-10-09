import shlex
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from sieve.core.history import recompute_repo_stats
from sieve.core.ingest import create_run
from sieve.core.junit import ParsedTestResult, Status
from sieve.core.schemas import RunMetadata
from sieve.core.selector import (
    DependsRule,
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Runner,
    Selection,
    SelectorConfig,
    build_file_scope,
    glob_match,
    is_build_file,
    load_history,
    pytest_file,
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
        "package-lock.json",
        "web/tsconfig.json",  # nested, but not a project manifest: still global
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
    assert selection.commands == ("pytest src/pkg/tests/test_cart.py",)


def test_python_module_with_custom_name_uses_file_path() -> None:
    # python_files = "check_*.py": the module isn't recognisable from the classname alone.
    hist = history(("tests.check_cart.CartChecks::test_total", "tests/check_cart.py"))
    selection = select(hist, "tests/check_cart.py")

    # Changed, so it runs whole; known by its file_path even without a test_*.py name.
    assert selection.commands == ("pytest tests/check_cart.py",)


# --- path mapping: Go ---------------------------------------------------------------------

GO = history(
    f"{CLI}::TestAdd",
    f"{CLI}::TestAdd/produced_cid_version:_implicit_default_(CIDv0)",
    f"{CLI}::TestPins/test_pinning/test_pins_with_args={{runDaemon:true}}",
    f"{COREUNIX}::TestAdd",
    f"{COREUNIX}::TestAdd/ipfs_add_--to-files",
    f"{KUBO}::TestCommands",
)
GO_IN = {
    package: [t for t in GO.tests if t.startswith(f"{package}::")] for package in (CLI, COREUNIX)
}


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
    assert selection.commands == ("go test .",)


def test_go_module_makes_package_matching_exact() -> None:
    # Without go_module, "cli" suffix-matches .../test/cli (safe over-selection).
    hist = history(f"{CLI}::TestAdd", f"{KUBO}/cli::TestMain")
    assert len(select(hist, "cli/main.go").tests) == 2
    assert ids(select(hist, "cli/main.go", go_module=KUBO)) == [f"{KUBO}/cli::TestMain"]


# --- Go dependents (affected_packages from go list) ---------------------------------------

COREAPI = f"{KUBO}/core/coreapi"  # a package with no tests of its own


def test_dependents_are_selected_and_run_whole() -> None:
    selection = select_tests(
        GO,
        ["core/coreapi/api.go"],
        True,
        SelectorConfig(go_module=KUBO),
        affected_packages={COREUNIX: [COREAPI], CLI: [COREAPI, f"{KUBO}/config"]},
    )

    # coreapi has no tests, but its importers do, so this is not a full-suite fallback.
    assert selection.mode is Mode.SELECTIVE
    assert reasons(selection)[f"{COREUNIX}::TestAdd"] == f"imports changed package {COREAPI}"
    # Several changed imports: the first (sorted) is named.
    assert (
        reasons(selection)[f"{CLI}::TestAdd"] == f"imports changed package {KUBO}/config (+1 more)"
    )
    assert selection.commands == ("go test ./core/coreunix", "go test ./test/cli")
    assert selection.go_packages_run_whole == (COREUNIX, CLI)


def test_dependents_without_known_tests_do_not_cover_a_change() -> None:
    selection = select_tests(
        GO,
        ["core/coreapi/api.go"],
        True,
        SelectorConfig(go_module=KUBO),
        affected_packages={f"{KUBO}/core/untested": [COREAPI]},
    )
    assert selection.reason == "changed file maps to no known tests: core/coreapi/api.go"


def test_dependents_only_cover_the_packages_they_import() -> None:
    # coreunix imports coreapi, but nothing covers the change in ./plugin.
    selection = select_tests(
        GO,
        ["core/coreapi/api.go", "plugin/loader.go"],
        True,
        SelectorConfig(go_module=KUBO),
        affected_packages={COREUNIX: [COREAPI]},
    )
    assert selection.reason == "changed file maps to no known tests: plugin/loader.go"


# --- declared dependencies (.sieve.toml) --------------------------------------------------

CLI_ON_GO = DependsRule("test/cli/**", ("**/*.go", "!**/*_test.go"))


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("core/coreapi/api.go", "**/*.go", True),
        ("version.go", "**/*.go", True),  # ** matches zero directories
        ("core/coreapi/api.go", "*.go", False),  # * stays within one segment
        ("core/api_test.go", "**/*_test.go", True),
        ("src/a/b/c.py", "src/**", True),
        ("src", "src/**", False),
        ("docs/a.md", "docs/?.md", True),
        ("test/cli/TestAdd", "test/cli/**", True),
        ("test/clix/TestAdd", "test/cli/**", False),
    ],
)
def test_glob_match(path: str, pattern: str, expected: bool) -> None:
    assert glob_match(path, pattern) is expected


def test_depends_rule_negation() -> None:
    assert CLI_ON_GO.matches_file("core/coreapi/api.go")
    assert not CLI_ON_GO.matches_file("core/coreapi/api_test.go")
    assert not CLI_ON_GO.matches_file("docs/config.md")


@pytest.mark.parametrize("go_module", [KUBO, None])
def test_declared_dependency_selects_binary_level_suite(go_module: str | None) -> None:
    # Works with or without go_module: "test/cli/**" matches a suffix of the import path.
    selection = select(GO, "core/coreunix/add.go", depends=(CLI_ON_GO,), go_module=go_module)

    assert set(ids(selection)) == {*GO_IN[COREUNIX], *GO_IN[CLI]}
    assert reasons(selection)[f"{CLI}::TestAdd"] == (
        "declared dependency: test/cli/** on core/coreunix/add.go"
    )
    assert selection.go_packages_run_whole == (COREUNIX, CLI)


def test_declared_dependency_covers_a_package_without_tests() -> None:
    selection = select(GO, "core/coreapi/api.go", depends=(CLI_ON_GO,), go_module=KUBO)

    assert selection.mode is Mode.SELECTIVE
    assert set(ids(selection)) == set(GO_IN[CLI])
    assert selection.commands == ("go test ./test/cli",)


def test_declared_dependency_respects_exclusions() -> None:
    selection = select(GO, "core/coreunix/add_test.go", depends=(CLI_ON_GO,), go_module=KUBO)
    assert set(ids(selection)) == set(GO_IN[COREUNIX])  # test-only change: not test/cli


def test_declared_dependency_matching_no_tests_does_not_cover() -> None:
    rule = DependsRule("e2e/**", ("**/*.go",))
    selection = select(GO, "core/coreapi/api.go", depends=(rule,), go_module=KUBO)
    assert selection.mode is Mode.FULL


def test_declared_dependency_for_python_file_paths() -> None:
    hist = history(
        ("tests.integration.test_api::test_flow", "tests/integration/test_api.py"),
        *PY.tests.values(),
    )
    rule = DependsRule("tests/integration/**", ("src/**",))
    selection = select(hist, "src/shop/helpers.py", depends=(rule,))

    assert ids(selection) == ["tests.integration.test_api::test_flow"]
    assert selection.commands == ("pytest tests/integration/test_api.py::test_flow",)


# --- nested build files -------------------------------------------------------------------

EXAMPLE = f"{KUBO}/docs/examples/kubo-as-a-library"  # a nested Go module
NESTED = history(
    *GO.tests.values(),
    f"{EXAMPLE}::TestLibrary",
    ("api.tests.test_routes::test_get", "services/api/tests/test_routes.py"),
    ("cart::adds", "web/src/cart.test.ts"),
    ("admin cart::adds", "web2/src/cart.test.ts"),
)


@pytest.mark.parametrize(
    ("path", "scope"),
    [
        ("docs/examples/kubo-as-a-library/go.mod", "docs/examples/kubo-as-a-library"),
        ("docs/examples/kubo-as-a-library/go.sum", "docs/examples/kubo-as-a-library"),
        ("web/package.json", "web"),
        ("web/yarn.lock", "web"),
        ("services/api/pyproject.toml", "services/api"),
        ("services/api/requirements-dev.txt", "services/api"),
        ("go.mod", None),  # root: whole repo
        ("package.json", None),
        ("pyproject.toml", None),
        ("docker/Dockerfile.dev", None),  # not a project manifest
        ("tests/conftest.py", None),
        ("src/cart.py", None),
    ],
)
def test_build_file_scope(path: str, scope: str | None) -> None:
    assert build_file_scope(path) == scope


def test_nested_go_module_runs_only_its_own_tests() -> None:
    selection = select(
        NESTED,
        "docs/examples/kubo-as-a-library/go.mod",
        "docs/examples/kubo-as-a-library/go.sum",
        go_module=KUBO,
    )

    assert selection.mode is Mode.SELECTIVE
    assert ids(selection) == [f"{EXAMPLE}::TestLibrary"]
    assert reasons(selection)[f"{EXAMPLE}::TestLibrary"] == (
        "build file docs/examples/kubo-as-a-library/go.mod changed "
        "(tests under docs/examples/kubo-as-a-library/); "
        "build file docs/examples/kubo-as-a-library/go.sum changed "
        "(tests under docs/examples/kubo-as-a-library/)"
    )
    assert selection.go_packages_run_whole == (EXAMPLE,)
    assert selection.commands == ("go test ./docs/examples/kubo-as-a-library",)


def test_nested_build_file_without_known_tests_does_not_force_full() -> None:
    selection = select(GO, "docs/examples/kubo-as-a-library/go.mod", go_module=KUBO)

    assert selection.mode is Mode.SELECTIVE
    assert selection.reason == "no tests affected"


def test_nested_build_file_combines_with_other_changes() -> None:
    selection = select(NESTED, "web/package.json", "core/coreunix/add.go", go_module=KUBO)

    assert set(ids(selection)) == {"cart::adds", *GO_IN[COREUNIX]}
    assert "admin cart::adds" not in ids(selection)  # web2/ is not under web/
    assert reasons(selection)["cart::adds"] == (
        "build file web/package.json changed (tests under web/)"
    )


def test_nested_python_project() -> None:
    selection = select(NESTED, "services/api/pyproject.toml")
    assert ids(selection) == ["api.tests.test_routes::test_get"]


@pytest.mark.parametrize("root_file", ["go.mod", "go.sum", "package.json", "pyproject.toml"])
def test_root_build_file_still_forces_full_suite(root_file: str) -> None:
    selection = select(NESTED, "docs/examples/kubo-as-a-library/go.mod", root_file)

    assert selection.mode is Mode.FULL
    assert selection.reason == f"build/config file changed: {root_file}"


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
    assert selection.reason == "no tests affected"


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


# --- pytest collection errors -------------------------------------------------------------

# A module that fails to import: pytest's JUnit has no classname, the suite is named "pytest"
# and the test name is the dotted module (rdflib's CI reports doctest modules this way).
SOURCE_MODULE_ERROR = "pytest::rdflib.plugins.serializers.n3"
TEST_MODULE_ERROR = "pytest::test.test_dataset.test_dataset_default_graph"


def test_collection_error_of_a_source_module_runs_that_module() -> None:
    # Co-change pulls it in: it failed in an earlier run that touched the same file.
    earlier = FailedRun(3, "c" * 40, frozenset({"rdflib/plugins/sparql/parser.py"}),
                        frozenset({SOURCE_MODULE_ERROR}))  # fmt: skip
    hist = history("test.test_sparql.test_parser::test_x", SOURCE_MODULE_ERROR,
                   failed_runs=(earlier,))  # fmt: skip
    selection = select(hist, "rdflib/plugins/sparql/parser.py")

    assert selection.mode is Mode.SELECTIVE
    assert ids(selection) == [SOURCE_MODULE_ERROR, "test.test_sparql.test_parser::test_x"]
    assert selection.commands == (
        "pytest rdflib/plugins/serializers/n3.py test/test_sparql/test_parser.py::test_x",
        # a collection error in a source module: the repo imports modules, so parser.py runs
        "{ pytest --doctest-modules rdflib/plugins/sparql/parser.py || test $? -eq 5; }",
    )
    [error] = [t for t in selection.tests if t.test_id == SOURCE_MODULE_ERROR]
    assert error.runner is Runner.PYTEST


def test_collection_error_of_a_test_module_is_selected_by_its_file() -> None:
    changed = "test/test_dataset/test_dataset_default_graph.py"
    selection = select(history(TEST_MODULE_ERROR, "test.test_other::test_y"), changed)

    assert selection.mode is Mode.SELECTIVE
    assert reasons(selection) == {TEST_MODULE_ERROR: f"test file changed: {changed}"}
    assert selection.commands == (f"pytest {changed}",)


def test_collection_error_file_replaces_node_ids_in_the_same_file() -> None:
    hist = history("pytest::test.test_cart", "test.test_cart::test_total",
                   recent_main_failures={"pytest::test.test_cart": (5, "e" * 40)})  # fmt: skip
    selection = select(hist, "src/cart.py")

    assert set(ids(selection)) == {"pytest::test.test_cart", "test.test_cart::test_total"}
    assert selection.commands == ("pytest test/test_cart.py",)  # not also ...::test_total


def test_collection_error_file_path_attribute_wins() -> None:
    hist = history(("pytest::shop.cart", "src/shop/cart.py"), "tests.test_cart::test_total",
                   recent_main_failures={"pytest::shop.cart": (5, "e" * 40)})  # fmt: skip
    assert select(hist, "src/shop/cart.py").commands == (
        "pytest src/shop/cart.py tests/test_cart.py::test_total",
        "{ pytest --doctest-modules src/shop/cart.py || test $? -eq 5; }",
    )


@pytest.mark.parametrize("name", ["not a module", "a/b.py", "", "a..b"])
def test_collection_error_without_a_derivable_file_is_left_out_of_the_command(name: str) -> None:
    # Selected (it failed on main), but there is no file to pass to pytest: skip it in the
    # command instead of forcing the full suite.
    error = f"pytest::{name}"
    hist = history(error, "tests.test_cart::test_total",
                   recent_main_failures={error: (5, "e" * 40)})  # fmt: skip
    selection = select(hist, "src/cart.py")

    assert selection.mode is Mode.SELECTIVE
    assert set(ids(selection)) == {error, "tests.test_cart::test_total"}
    assert selection.commands == ("pytest tests/test_cart.py::test_total",)


def test_only_underivable_collection_errors_give_an_empty_command() -> None:
    error = "pytest::not a module"
    hist = history(error, "tests.test_cart::test_total",
                   recent_main_failures={error: (5, "e" * 40)})  # fmt: skip
    selection = select(hist, "README.md")

    assert (selection.mode, ids(selection), selection.command) == (Mode.SELECTIVE, [error], "")


# --- doctests -----------------------------------------------------------------------------

DOCTEST = "rdflib.container::rdflib.container.Container"
MODULE_DOCTEST = "rdflib.container::rdflib.container"


def test_doctests_run_their_module_with_doctest_modules() -> None:
    hist = history(DOCTEST, MODULE_DOCTEST, "test.test_cart::test_total",
                   recent_main_failures={DOCTEST: (5, "e" * 40)})  # fmt: skip
    selection = select(hist, "rdflib/container.py", "src/cart.py")

    assert selection.mode is Mode.SELECTIVE
    assert set(ids(selection)) == {DOCTEST, MODULE_DOCTEST, "test.test_cart::test_total"}
    assert reasons(selection)[MODULE_DOCTEST] == "test file changed: rdflib/container.py"
    assert selection.commands == (
        "pytest test/test_cart.py::test_total",
        # rdflib/container.py has known doctests; src/cart.py is imported for any it has
        "{ pytest --doctest-modules rdflib/container.py src/cart.py || test $? -eq 5; }",
    )
    assert "rdflib/container.py" in selection.python_files_run_whole


def test_changed_modules_run_with_doctest_modules_when_the_repo_collects_modules() -> None:
    # History has a doctest, so the repo's pytest imports source modules: a new module that
    # fails to import would show up as a collection error, so it must run.
    hist = history(DOCTEST, "tests.test_cart::test_total")
    affected = {"tests/test_cart.py": ["rdflib/inference/closure.py"]}
    selection = select_tests(hist, ["rdflib/inference/closure.py", "tests/test_cart.py"], True,
                             affected_files=affected)  # fmt: skip

    assert selection.mode is Mode.SELECTIVE, selection.reason
    assert selection.commands == (
        "pytest tests/test_cart.py",
        # no doctests in the new module: exit 5 is fine; import errors (2) still fail
        "{ pytest --doctest-modules rdflib/inference/closure.py || test $? -eq 5; }",
    )
    assert "rdflib/inference/closure.py" in selection.python_files_run_whole


def test_collection_error_in_a_source_module_also_shows_the_repo_collects_modules() -> None:
    hist = history(SOURCE_MODULE_ERROR, "tests.test_cart::test_total")
    selection = select_tests(hist, ["src/cart.py"], True)
    assert selection.commands[-1].startswith("{ pytest --doctest-modules src/cart.py")


def test_changed_modules_are_not_imported_when_the_repo_does_not_collect_modules() -> None:
    # Only a collection error in a *test* module: no evidence of --doctest-modules.
    hist = history(TEST_MODULE_ERROR, "tests.test_cart::test_total")
    selection = select_tests(hist, ["src/cart.py"], True)
    assert selection.commands == ("pytest tests/test_cart.py::test_total",)


@pytest.mark.parametrize("test_id", ["my-pkg.mod::my-pkg.mod.f", "a..b::a..b.f"])
def test_doctest_without_a_derivable_file_is_left_out_of_the_command(test_id: str) -> None:
    hist = history(test_id, "tests.test_cart::test_total",
                   recent_main_failures={test_id: (5, "e" * 40)})  # fmt: skip
    selection = select(hist, "src/cart.py")

    assert selection.mode is Mode.SELECTIVE, selection.reason
    assert selection.commands == ("pytest tests/test_cart.py::test_total",)


@pytest.mark.parametrize(
    "test_id",
    [
        "tests.test_cart::test_total",  # a test function
        "tests.test_cart::test_param[tests.test_cart.x]",
        "com.acme.CartTest::testTotal",  # Java
        f"{CLI}::TestAdd",  # Go
        "Cart adds item::Cart adds item",  # jest-junit: classname = name, with spaces
    ],
)
def test_ordinary_tests_are_not_doctests(test_id: str) -> None:
    hist = history(test_id, recent_main_failures={test_id: (5, "e" * 40)})
    selection = select(hist, "README.md")
    assert "--doctest-modules" not in selection.command


# --- Python test files and imports --------------------------------------------------------

CART = "tests.test_cart::test_total"
CART_CLASS = "tests.test_cart.TestDiscount::test_applies"
IO = "tests.test_io::test_read"
DEEP = "tests.unit.test_report::test_sum"


def test_changed_test_file_runs_whole() -> None:
    selection = select(history(CART, CART_CLASS, IO), "tests/test_cart.py")

    assert set(ids(selection)) == {CART, CART_CLASS}
    assert selection.commands == ("pytest tests/test_cart.py",)  # new tests in it run too
    assert selection.python_files_run_whole == ("tests/test_cart.py",)


def test_added_test_file_runs_whole_without_known_tests() -> None:
    selection = select(history(CART, IO), "tests/test_new.py")

    assert selection.mode is Mode.SELECTIVE  # not "maps to no known tests"
    assert ids(selection) == []
    assert selection.reason == "1 changed test file(s) run whole, no known tests"
    assert selection.commands == ("pytest tests/test_new.py",)


def test_deleted_test_file_runs_nothing() -> None:
    # Its tests failed on main, but the file is gone: naming it would make pytest error out.
    hist = history(CART, IO, recent_main_failures={CART: (5, "e" * 40)})
    selection = select_tests(hist, ["tests/test_cart.py", "tests/test_io.py"], True,
                             deleted_files=["tests/test_cart.py"])  # fmt: skip

    assert ids(selection) == [IO]
    assert selection.commands == ("pytest tests/test_io.py",)


def test_test_files_importing_a_changed_module_run_whole() -> None:
    hist = history(CART, IO, DEEP)
    affected = {"tests/test_io.py": ["src/shop/money.py", "src/shop/cart.py"],
                "tests/unit/test_report.py": ["src/shop/money.py"]}  # fmt: skip
    selection = select_tests(hist, ["src/shop/money.py"], True, affected_files=affected)

    # money.py has no test_money.py: without the import graph this was the full suite.
    assert selection.mode is Mode.SELECTIVE
    assert reasons(selection) == {
        IO: "imports changed module src/shop/cart.py (+1 more)",
        DEEP: "imports changed module src/shop/money.py",
    }
    assert selection.commands == ("pytest tests/test_io.py tests/unit/test_report.py",)
    assert selection.python_files_run_whole == ("tests/test_io.py", "tests/unit/test_report.py")


def test_imports_only_cover_the_modules_they_import() -> None:
    affected = {"tests/test_io.py": ["src/shop/money.py"]}
    selection = select_tests(history(CART, IO), ["src/shop/money.py", "src/shop/tax.py"], True,
                             affected_files=affected)  # fmt: skip

    assert selection.mode is Mode.FULL
    assert selection.reason == "changed file maps to no known tests: src/shop/tax.py"


def test_importing_test_files_without_known_tests_do_not_cover_a_change() -> None:
    # An unknown test file may not even be collected by pytest: don't name it, stay safe.
    affected = {"tests/test_unknown.py": ["src/shop/money.py"]}
    selection = select_tests(history(CART), ["src/shop/money.py"], True, affected_files=affected)

    assert selection.mode is Mode.FULL


def test_whole_files_replace_node_ids_from_other_signals() -> None:
    hist = history(CART, IO, recent_main_failures={CART: (5, "e" * 40)})
    affected = {"tests/test_cart.py": ["src/shop/money.py"]}
    selection = select_tests(hist, ["src/shop/money.py"], True, affected_files=affected)

    assert selection.commands == ("pytest tests/test_cart.py",)


@pytest.mark.parametrize(
    ("test_id", "expected"),
    [
        (CART, "tests/test_cart.py"),
        (CART_CLASS, "tests/test_cart.py"),
        (DOCTEST, "rdflib/container.py"),
        (SOURCE_MODULE_ERROR, "rdflib/plugins/serializers/n3.py"),
        (f"{CLI}::TestAdd", None),
    ],
)
def test_pytest_file(test_id: str, expected: str | None) -> None:
    assert pytest_file(test_id) == expected


def test_go_packages_with_changed_files_run_whole() -> None:
    # No -run: tests added in this change aren't in history yet but must still run.
    selection = select(GO, "test/cli/add.go", "core/coreunix/add.go", go_module=KUBO)

    assert selection.commands == ("go test ./core/coreunix", "go test ./test/cli")
    assert selection.go_packages_run_whole == (COREUNIX, CLI)
    assert selection.command == " && ".join(selection.commands)


def test_go_tests_from_other_signals_keep_run_filter_by_top_level_test() -> None:
    hist = history(
        *GO.tests.values(),
        recent_main_failures={
            f"{COREUNIX}::TestAdd/ipfs_add_--to-files": (3, "a" * 40),  # subtest -> TestAdd
            f"{CLI}::TestPins/test_pinning/test_pins_with_args={{runDaemon:true}}": (3, "a" * 40),
        },
    )
    selection = select(hist, "core/coreunix/add.go", go_module=KUBO)

    # coreunix has a changed file: whole package. test/cli is only in via a recent failure.
    assert selection.commands == (
        "go test ./core/coreunix",
        "go test ./test/cli -run '^(TestPins)$'",
    )
    assert selection.go_packages_run_whole == (COREUNIX,)


def test_go_package_selected_only_by_other_signals_is_not_run_whole() -> None:
    hist = history(*GO.tests.values(), recent_main_failures={f"{CLI}::TestAdd": (3, "a" * 40)})
    selection = select(hist, "README.md", go_module=KUBO)

    assert selection.commands == ("go test ./test/cli -run '^(TestAdd)$'",)
    assert selection.go_packages_run_whole == ()


def test_go_commands_use_import_paths_without_go_module() -> None:
    selection = select(GO, "test/cli/add.go")
    assert selection.commands == (f"go test {CLI}",)

    hist = history(*GO.tests.values(), recent_main_failures={f"{CLI}::TestAdd": (3, "a" * 40)})
    assert select(hist, "README.md").commands == (f"go test {CLI} -run '^(TestAdd)$'",)


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
    ci_run_id: str | None = None,
    variant: str | None = None,
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
        ci_run_id=ci_run_id,
        variant=variant,
    )
    run, _ = create_run(session, meta, results)
    return run.id


P, F = Status.PASSED, Status.FAILED


MATRIX_LEGS = [f"py3.{minor}-{os}" for minor in (10, 11, 12, 13, 14)
               for os in ("ubuntu", "macos", "windows", "ubuntu-extensive")]  # fmt: skip


def ingest_matrix(session: Session, n: int, tests: dict[str, Status], **kwargs: Any) -> None:
    """One CI run uploaded as 20 matrix variants (one run row each)."""
    for leg in MATRIX_LEGS:
        ingest(session, n, tests, ci_run_id=str(n), variant=leg, **kwargs)


def test_known_tests_window_counts_ci_runs_not_variants(db_session: Session) -> None:
    assert len(MATRIX_LEGS) == 20
    ingest_matrix(db_session, 1, {"tests.test_a::test_x": P, "tests.test_old::test_gone": P})
    ingest_matrix(db_session, 2, {"tests.test_a::test_x": P})

    # 2 CI runs = 40 uploads. Counting uploads, "last 2 runs" would be two legs of CI run 2.
    two = load_history(db_session, REPO, SelectorConfig(known_test_runs=2))
    one = load_history(db_session, REPO, SelectorConfig(known_test_runs=1))
    assert two is not None and one is not None
    assert set(two.tests) == {"tests.test_a::test_x", "tests.test_old::test_gone"}
    assert set(one.tests) == {"tests.test_a::test_x"}


def test_recent_main_failures_window_counts_ci_runs(db_session: Session) -> None:
    # CI run 1 fails on one leg; CI run 2 passes on all 20 legs.
    for leg in MATRIX_LEGS:
        status = F if leg == "py3.12-macos" else P
        ingest(db_session, 1, {"tests.test_a::test_x": status}, ci_run_id="1", variant=leg)
    ingest_matrix(db_session, 2, {"tests.test_a::test_x": P})

    two = load_history(db_session, REPO, SelectorConfig(recent_main_runs=2))
    one = load_history(db_session, REPO, SelectorConfig(recent_main_runs=1))
    assert two is not None and one is not None
    assert set(two.recent_main_failures) == {"tests.test_a::test_x"}
    assert one.recent_main_failures == {}


def test_co_change_window_counts_ci_runs(db_session: Session) -> None:
    for leg in MATRIX_LEGS:
        status = F if leg == "py3.10-ubuntu" else P
        ingest(db_session, 1, {"tests.test_a::test_x": status}, ci_run_id="1", variant=leg,
               changed=("src/a.py",))  # fmt: skip
    ingest_matrix(db_session, 2, {"tests.test_a::test_x": P}, changed=("src/b.py",))

    hist = load_history(db_session, REPO, SelectorConfig(co_change_runs=2))
    assert hist is not None
    assert [r.failed_tests for r in hist.failed_runs] == [frozenset({"tests.test_a::test_x"})]


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


def _test_result_rows_read(plan: dict[str, Any]) -> int:
    """Rows a plan read from test_results: returned, plus any read and then filtered out."""
    rows = 0
    if plan.get("Relation Name") == "test_results":
        loops = plan.get("Actual Loops", 0)
        for key in ("Actual Rows", "Rows Removed by Filter", "Rows Removed by Index Recheck"):
            rows += plan.get(key, 0) * loops
    return rows + sum(_test_result_rows_read(child) for child in plan.get("Plans", []))


def test_load_history_does_not_read_passing_raw_results(db_session: Session) -> None:
    # 30 CI runs x 4 matrix legs x 150 passing tests = 18,000 passing rows, all in the windows
    # load_history looks at, plus one failure per CI run.
    passing = {f"tests.test_m{i}::test_ok": P for i in range(150)}
    for n in range(1, 31):
        for leg in ("a", "b", "c", "d"):
            tests = dict(passing)
            if leg == "a":
                tests[f"tests.test_fail::test_{n % 3}"] = F
            results = [ParsedTestResult(t, t.split("::")[0], t.split("::")[1], None, s, 1, 1)
                       for t, s in tests.items()]  # fmt: skip
            meta = RunMetadata(repo=REPO, commit_sha=f"{n:040x}", branch="b", is_main=n % 2 == 0,
                               started_at=T0 + timedelta(hours=n), ci_run_id=str(n),
                               variant=leg, changed_files=[f"src/f{n % 5}.py"])  # fmt: skip
            run, _ = create_run(db_session, meta, results, rollup=False)  # batch ingest
    recompute_repo_stats(db_session, run.repo_id, 90)
    db_session.execute(text("ANALYZE test_results"))
    db_session.execute(text("ANALYZE runs"))
    db_session.execute(text("ANALYZE test_stats"))
    failing_rows = db_session.scalar(
        text("SELECT count(*) FROM test_results WHERE status IN ('failed', 'error')")
    )

    statements: list[tuple[str, Any]] = []
    connection = db_session.connection()

    def record(conn: Any, cursor: Any, statement: str, params: Any, *_: Any) -> None:
        statements.append((statement, params))

    event.listen(connection, "before_cursor_execute", record)
    try:
        hist = load_history(db_session, REPO, SelectorConfig(known_test_runs=5))
    finally:
        event.remove(connection, "before_cursor_execute", record)

    assert hist is not None
    assert len(hist.tests) == 153  # 150 passing tests + the 3 failing ones
    assert len(hist.failed_runs) == 30
    assert set(hist.recent_main_failures) == {f"tests.test_fail::test_{k}" for k in range(3)}

    read = 0
    for statement, params in statements:
        if "test_results" in statement:
            plan = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params
            ).scalar_one()[0]["Plan"]
            read += _test_result_rows_read(plan)
    assert failing_rows == 30
    assert read <= 2 * failing_rows  # failures only (co-change + recent main); never 18,000


def test_known_tests_come_from_test_stats(db_session: Session) -> None:
    ingest(db_session, 1, {"tests.test_a::test_x": P})
    # Rows the rollup never saw (no stats) are not known: load_history doesn't read them.
    run = ingest(db_session, 2, {"tests.test_a::test_x": P})
    db_session.execute(
        text(
            "INSERT INTO test_results (run_id, test_id, status, attempt) "
            "VALUES (:run, 'tests.test_raw::only', 'passed', 1)"
        ),
        {"run": run},
    )
    hist = load_history(db_session, REPO)
    assert hist is not None
    assert set(hist.tests) == {"tests.test_a::test_x"}


def test_known_tests_keep_the_latest_file_path(db_session: Session) -> None:
    for n, path in ((1, "tests/old/test_a.py"), (2, "tests/test_a.py"), (3, None)):
        meta = RunMetadata(repo=REPO, commit_sha=f"{n:040x}", branch="main", is_main=True,
                           started_at=T0 + timedelta(hours=n))  # fmt: skip
        result = ParsedTestResult("tests.test_a::test_x", "tests.test_a", "test_x", path, P, 1, 1)
        create_run(db_session, meta, [result])

    hist = load_history(db_session, REPO)
    assert hist is not None
    assert hist.tests["tests.test_a::test_x"] == KnownTest(
        "tests.test_a::test_x", "tests/test_a.py"
    )


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
