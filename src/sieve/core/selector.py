"""Test selection: which tests to run for a change.

``select_tests`` is pure: given a repo's ``RepoHistory`` and a change, it returns a
``Selection``. ``load_history`` builds that history from the database, and ``select_for_repo``
does both.

Missing a real failure is worse than running extra tests, so whenever the selector is unsure
it returns ``Mode.FULL`` with a reason. It falls back to the full suite when:

* the changed files are unknown, or the list is empty;
* a build, dependency, CI or test-config file changed;
* the repo has no test history;
* any changed source file maps to no known test (non-code files like docs are ignored);
* a selected test has no known runner, so no command can be built for it.

Otherwise it selects the union of these signals, and every test carries its reasons:

* **Path mapping**, language-aware:
  - Python: ``src/x/foo.py`` selects tests in any ``test_foo.py`` / ``foo_test.py``. Test file
    paths come from ``file_path``, or are derived from the pytest classname
    (``tests.test_foo.TestBar`` -> ``tests/test_foo.py``).
  - Go: a changed ``.go`` file selects every test in the same package directory, and that
    package runs whole (no ``-run``), so tests added in the change run too. Packages are
    matched by import-path suffix (``test/cli`` matches ``github.com/ipfs/kubo/test/cli``), or
    exactly when ``SelectorConfig.go_module`` is set.
  - JS/TS: ``foo.ts`` selects ``foo.test.*``, ``foo.spec.*`` and ``__tests__/foo.*``.
  - A changed test file always selects its own tests.
* **Co-change**: tests that failed in recent runs whose changed files overlap this change.
* **Recently failed**: tests that failed in the last ``recent_main_runs`` main runs, or are
  currently broken on main.
* **Always-run**: ``always_run`` glob patterns, matched against test IDs and file paths.

Matching is deliberately loose (by file name or package suffix anywhere in the repo): extra
tests are cheap, missed ones are not. Path mapping only sees direct naming relationships, not
imports, so a change can still break a test in a dependent module or package. Co-change and
recently-failed partly cover that. The kubo real-data check measures what is missed.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

from sieve.core.junit import normalize_path


class Mode(StrEnum):
    FULL = "full"
    SELECTIVE = "selective"


class Runner(StrEnum):
    PYTEST = "pytest"
    GO = "go"
    JEST = "jest"


# Full-suite commands, in the order commands are emitted.
FULL_SUITE_COMMANDS: dict[Runner, str] = {
    Runner.PYTEST: "pytest",
    Runner.GO: "go test ./...",
    Runner.JEST: "jest",
}

# Reason for a selective result with nothing to run (e.g. a docs-only change).
NO_TESTS_AFFECTED = "no tests affected"

JS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"})

# Changing any of these can affect every test: dependencies, build, CI, test configuration.
_BUILD_BASENAMES = frozenset(
    {
        # Python
        "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock",
        "uv.lock", "pdm.lock", "tox.ini", "pytest.ini", "conftest.py", "noxfile.py",
        # JS/TS
        "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
        "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lockb", "bun.lock", ".babelrc", ".nvmrc",
        # Go
        "go.mod", "go.sum", "go.work", "go.work.sum",
        # Build / CI
        "Dockerfile", "Makefile", ".gitlab-ci.yml",
    }
)  # fmt: skip
_BUILD_GLOBS = (
    "requirements*.txt", "Dockerfile.*", "*.Dockerfile", "docker-compose*.yml",
    "docker-compose*.yaml", "tsconfig*.json", "jest.config.*", "babel.config.*",
    "vitest.config.*",
)  # fmt: skip
_BUILD_DIRS = (".github/workflows/", ".github/actions/")

# Files that cannot affect test outcomes. They never trigger the full suite on their own.
_IGNORABLE_EXTENSIONS = frozenset(
    {".md", ".markdown", ".rst", ".adoc", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp"}
)
_IGNORABLE_BASENAMES = frozenset(
    {
        "LICENSE", "NOTICE", "AUTHORS", "CODEOWNERS", "CONTRIBUTORS", "CHANGELOG",
        ".gitignore", ".gitattributes", ".editorconfig", ".mailmap",
    }
)  # fmt: skip
_IGNORABLE_DIRS = ("docs/",)

_PY_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")
_GO_TOP_LEVEL = re.compile(r"(Test|Fuzz|Example|Benchmark)")


@dataclass(frozen=True)
class SelectorConfig:
    always_run: tuple[str, ...] = ()
    recent_main_runs: int = 10
    # How many recent runs to search for co-change failures.
    co_change_runs: int = 500
    # A test is "known" only if it appeared in one of the repo's latest N runs, so deleted or
    # renamed tests are never selected (pytest errors on node IDs that no longer exist).
    known_test_runs: int = 50
    # Go module path (e.g. "github.com/ipfs/kubo"). When set, Go commands use ./relative
    # package dirs and root-package changes can be mapped; otherwise import paths are used.
    go_module: str | None = None


@dataclass(frozen=True)
class KnownTest:
    test_id: str
    file_path: str | None = None


@dataclass(frozen=True)
class FailedRun:
    run_id: int
    commit_sha: str
    changed_files: frozenset[str]
    failed_tests: frozenset[str]


@dataclass(frozen=True)
class RepoHistory:
    tests: Mapping[str, KnownTest]
    # test_id -> first sha of the current failing streak on main
    broken_on_main: Mapping[str, str] = field(default_factory=dict)
    # test_id -> (run_id, commit_sha) of its most recent failure among the recent main runs
    recent_main_failures: Mapping[str, tuple[int, str]] = field(default_factory=dict)
    # Recent runs (changed_files_known only) with at least one failure, newest first.
    failed_runs: Sequence[FailedRun] = ()


@dataclass(frozen=True)
class SelectedTest:
    test_id: str
    reasons: tuple[str, ...]
    runner: Runner | None
    file_path: str | None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


@dataclass(frozen=True)
class Selection:
    mode: Mode
    # Why the full suite was chosen, or a one-line summary of a selective run.
    reason: str
    tests: tuple[SelectedTest, ...]
    selected_count: int
    total_known: int
    commands: tuple[str, ...]
    # Go packages with changed .go files. Their command has no -run filter, so tests added in
    # the same change (not yet in history) run too.
    go_packages_run_whole: tuple[str, ...] = ()

    @property
    def command(self) -> str:
        """All commands as one shell line (empty if nothing needs to run)."""
        return " && ".join(self.commands)


# --- selection ----------------------------------------------------------------------------


def select_tests(
    history: RepoHistory | None,
    changed_files: Iterable[str],
    changed_files_known: bool,
    config: SelectorConfig | None = None,
) -> Selection:
    config = config or SelectorConfig()
    tests = _index(history.tests.values() if history else ())

    def full(reason: str) -> Selection:
        runners = {t.runner for t in tests.values() if t.runner is not None}
        return Selection(
            mode=Mode.FULL,
            reason=reason,
            tests=(),
            selected_count=len(tests),
            total_known=len(tests),
            commands=tuple(cmd for r, cmd in FULL_SUITE_COMMANDS.items() if r in runners),
        )

    if not changed_files_known:
        return full("changed files are unknown for this change")
    paths = sorted({p for p in (normalize_path(f) for f in changed_files) if p})
    if not paths:
        return full("no changed files were given")
    build = [p for p in paths if is_build_file(p)]
    if build:
        return full(f"build/config file changed: {_list(build)}")
    if history is None or not tests:
        return full("no test history for this repo")

    selected: dict[str, list[str]] = {}

    def add(test_id: str, reason: str) -> None:
        reasons = selected.setdefault(test_id, [])
        if reason not in reasons:
            reasons.append(reason)

    # Path mapping. Every changed source file must map to at least one test.
    relevant = [p for p in paths if not is_ignorable(p)]
    unmapped = []
    whole_packages: set[str] = set()
    for path in relevant:
        hits = _map_path(path, tests, config)
        if not hits:
            unmapped.append(path)
        for test_id, reason in hits.items():
            add(test_id, reason)
            if path.endswith(".go") and tests[test_id].runner is Runner.GO:
                whole_packages.add(tests[test_id].classname)
    if unmapped:
        return full(f"changed file maps to no known tests: {_list(unmapped)}")

    # Co-change: earlier failures in runs that touched the same files. One reason per test,
    # from the most recent such run.
    relevant_set = frozenset(relevant)
    co_changed: set[str] = set()
    for run in history.failed_runs:
        overlap = run.changed_files & relevant_set
        if not overlap:
            continue
        for test_id in sorted(run.failed_tests - co_changed):
            if test_id in tests:
                co_changed.add(test_id)
                add(
                    test_id,
                    f"co-change: failed in run {run.run_id} ({run.commit_sha[:7]}), "
                    f"which also changed {_list(sorted(overlap))}",
                )

    # Recently failed or broken on main.
    for test_id, sha in sorted(history.broken_on_main.items()):
        if test_id in tests:
            add(test_id, f"broken on main since {sha[:7]}")
    for test_id, (run_id, sha) in sorted(history.recent_main_failures.items()):
        if test_id in tests:
            add(test_id, f"failed on main in run {run_id} ({sha[:7]})")

    # Always-run patterns.
    for pattern in config.always_run:
        for test in tests.values():
            if fnmatch.fnmatchcase(test.test_id, pattern) or (
                test.file_path and fnmatch.fnmatchcase(test.file_path, pattern)
            ):
                add(test.test_id, f"always-run pattern {pattern!r}")

    chosen = [tests[test_id] for test_id in sorted(selected)]
    unrunnable = [t.test_id for t in chosen if t.runner is None]
    if unrunnable:
        return full(f"no known test runner for selected test(s): {_list(unrunnable)}")

    return Selection(
        mode=Mode.SELECTIVE,
        reason=(
            f"{len(chosen)} of {len(tests)} known tests selected" if chosen else NO_TESTS_AFFECTED
        ),
        tests=tuple(
            SelectedTest(t.test_id, tuple(selected[t.test_id]), t.runner, t.file_path)
            for t in chosen
        ),
        selected_count=len(chosen),
        total_known=len(tests),
        commands=_build_commands(chosen, config.go_module, whole_packages),
        go_packages_run_whole=tuple(sorted(whole_packages)),
    )


def is_build_file(path: str) -> bool:
    base = posixpath.basename(path)
    return (
        base in _BUILD_BASENAMES
        or any(fnmatch.fnmatchcase(base, glob) for glob in _BUILD_GLOBS)
        or path.startswith(_BUILD_DIRS)
    )


def is_ignorable(path: str) -> bool:
    base = posixpath.basename(path)
    stem, ext = posixpath.splitext(base)
    return (
        ext.lower() in _IGNORABLE_EXTENSIONS
        or base in _IGNORABLE_BASENAMES
        or stem in _IGNORABLE_BASENAMES  # LICENSE.txt, CHANGELOG.txt
        or path.startswith(_IGNORABLE_DIRS)
    )


def _list(items: Sequence[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} (+{len(items) - limit} more)"


# --- test classification ------------------------------------------------------------------


@dataclass(frozen=True)
class _Test:
    test_id: str
    classname: str
    name: str
    file_path: str | None
    runner: Runner | None
    # Python only: path of the test module, and classes between module and function.
    py_path: str | None = None
    py_classes: tuple[str, ...] = ()


def _index(known: Iterable[KnownTest]) -> dict[str, _Test]:
    return {k.test_id: _classify(k) for k in known}


def _classify(test: KnownTest) -> _Test:
    classname, _, name = test.test_id.partition("::")
    file_path = normalize_path(test.file_path)
    ext = posixpath.splitext(file_path or "")[1]

    python = _python_location(classname, file_path)
    if ext == ".go" or (not ext and _looks_like_go(classname, name)):
        runner: Runner | None = Runner.GO
    elif ext in JS_EXTENSIONS:
        runner = Runner.JEST
    elif python is not None:
        runner = Runner.PYTEST
    else:
        runner = None

    if runner is Runner.PYTEST and python is not None:
        return _Test(test.test_id, classname, name, file_path, runner, python[0], python[1])
    return _Test(test.test_id, classname, name, file_path, runner)


def _looks_like_go(classname: str, name: str) -> bool:
    # go-junit-report / gotestsum: classname is the package import path, name is TestX[/sub].
    return (
        "/" in classname
        and not any(c.isspace() for c in classname)
        and _GO_TOP_LEVEL.match(name) is not None
    )


def _is_python_test_module(module: str) -> bool:
    return module.startswith("test_") or module.endswith("_test")


def _python_location(classname: str, file_path: str | None) -> tuple[str, tuple[str, ...]] | None:
    """``(test module path, classes)`` for a pytest classname, or None if it isn't one.

    ``tests.unit.test_foo.TestBar`` -> ``("tests/unit/test_foo.py", ("TestBar",))``.
    """
    parts = classname.split(".")
    if not classname or not all(_PY_IDENTIFIER.match(p) for p in parts):
        return None
    py_file = file_path if file_path and file_path.endswith(".py") else None
    module_end = max((i for i, p in enumerate(parts) if _is_python_test_module(p)), default=None)
    if module_end is None:
        if py_file is None:
            return None
        # Module name isn't recognisable; classes are the trailing CamelCase components.
        classes: list[str] = []
        for part in reversed(parts):
            if not part[:1].isupper():
                break
            classes.insert(0, part)
        return py_file, tuple(classes)
    path = py_file or "/".join(parts[: module_end + 1]) + ".py"
    return path, tuple(parts[module_end + 1 :])


# --- path mapping -------------------------------------------------------------------------


def _map_path(path: str, tests: Mapping[str, _Test], config: SelectorConfig) -> dict[str, str]:
    """test_id -> reason, for every known test this changed file maps to."""
    hits: dict[str, str] = {}
    base = posixpath.basename(path)
    stem, ext = posixpath.splitext(base)

    for t in tests.values():
        if path in (t.file_path, t.py_path):
            hits[t.test_id] = f"test file changed: {path}"

    if ext == ".py" and not _is_python_test_module(stem):
        targets = {f"test_{stem}.py", f"{stem}_test.py"}
        for t in tests.values():
            if t.py_path and posixpath.basename(t.py_path) in targets:
                hits.setdefault(t.test_id, f"{t.py_path} tests changed {path}")
    elif ext == ".go":
        directory = posixpath.dirname(path)
        for t in tests.values():
            if t.runner is Runner.GO and _go_package_matches(
                t.classname, directory, config.go_module
            ):
                hits.setdefault(t.test_id, f"in Go package {t.classname}, which has changed {path}")
    elif ext in JS_EXTENSIONS and not _is_js_test_file(path):
        for t in tests.values():
            if t.file_path and _js_test_matches(t.file_path, stem):
                hits.setdefault(t.test_id, f"{t.file_path} tests changed {path}")
    return hits


def _go_package_matches(package: str, directory: str, module: str | None) -> bool:
    if module and (package == module or package.startswith(module + "/")):
        return package[len(module) :].lstrip("/") == directory
    # No module path: match the directory as an import-path suffix. May over-select a
    # same-named directory elsewhere, which is safe. Root files need go_module to map.
    return bool(directory) and (package == directory or package.endswith("/" + directory))


def _is_js_test_file(path: str) -> bool:
    name = posixpath.splitext(posixpath.basename(path))[0]
    return name.endswith((".test", ".spec")) or "__tests__" in path.split("/")


def _js_test_matches(test_path: str, stem: str) -> bool:
    name, ext = posixpath.splitext(posixpath.basename(test_path))
    if ext not in JS_EXTENSIONS:
        return False
    if name in (f"{stem}.test", f"{stem}.spec"):
        return True
    return "__tests__" in test_path.split("/") and name == stem


# --- commands -----------------------------------------------------------------------------


def _build_commands(
    tests: Sequence[_Test],
    go_module: str | None = None,
    go_whole_packages: Collection[str] = (),
) -> tuple[str, ...]:
    """One command per runner (per package for Go), in a stable order.

    Go packages in ``go_whole_packages`` run without -run, so tests that history doesn't
    know yet (added in this change) run too. Other Go packages only had tests pulled in by
    co-change, recently-failed or always-run, so they keep -run for just those tests.
    """
    commands: list[str] = []

    pytest_ids = sorted(
        "::".join((t.py_path, *t.py_classes, t.name))
        for t in tests
        if t.runner is Runner.PYTEST and t.py_path
    )
    if pytest_ids:
        commands.append(shlex.join(["pytest", *pytest_ids]))

    # Go: -run matches top-level tests; subtests run as part of their parent.
    go_tests: dict[str, set[str]] = {}
    for t in tests:
        if t.runner is Runner.GO:
            go_tests.setdefault(t.classname, set()).add(t.name.split("/", 1)[0])
    for package in sorted(go_tests):
        target = _go_target(package, go_module)
        if package in go_whole_packages:
            commands.append(shlex.join(["go", "test", target]))
        else:
            pattern = "^(" + "|".join(sorted(go_tests[package])) + ")$"
            commands.append(shlex.join(["go", "test", target, "-run", pattern]))

    jest_files = sorted({t.file_path for t in tests if t.runner is Runner.JEST and t.file_path})
    if jest_files:
        commands.append(shlex.join(["jest", *jest_files]))
    return tuple(commands)


def _go_target(package: str, module: str | None) -> str:
    if module and package == module:
        return "."
    if module and package.startswith(module + "/"):
        return "./" + package[len(module) + 1 :]
    return package  # import paths work with `go test` from inside the module


# --- history from the database ------------------------------------------------------------


def load_history(
    session: Session, repo: str, config: SelectorConfig | None = None
) -> RepoHistory | None:
    """The selector's view of ``repo``'s history, or None if the repo is unknown."""
    config = config or SelectorConfig()
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    params = {"repo_id": repo_id}

    known = session.execute(
        text(
            """
            WITH recent AS (
                SELECT id FROM runs WHERE repo_id = :repo_id
                ORDER BY COALESCE(started_at, created_at) DESC, id DESC
                LIMIT :n
            )
            SELECT DISTINCT ON (tr.test_id) tr.test_id, tr.file_path
            FROM test_results tr JOIN recent ON recent.id = tr.run_id
            ORDER BY tr.test_id, tr.file_path IS NULL, tr.run_id DESC
            """
        ),
        {**params, "n": config.known_test_runs},
    ).all()

    broken = session.execute(
        text(
            "SELECT test_id, broken_on_main_since_sha FROM test_stats "
            "WHERE repo_id = :repo_id AND broken_on_main_since_sha IS NOT NULL"
        ),
        params,
    ).all()

    recent_failures = session.execute(
        text(
            """
            WITH recent AS (
                SELECT id, commit_sha, COALESCE(started_at, created_at) AS run_at
                FROM runs WHERE repo_id = :repo_id AND is_main
                ORDER BY run_at DESC, id DESC
                LIMIT :n
            )
            SELECT DISTINCT ON (tr.test_id) tr.test_id, recent.id, recent.commit_sha
            FROM recent JOIN test_results tr ON tr.run_id = recent.id
            WHERE tr.status IN ('failed', 'error')
            ORDER BY tr.test_id, recent.run_at DESC, recent.id DESC
            """
        ),
        {**params, "n": config.recent_main_runs},
    ).all()

    failed = session.execute(
        text(
            """
            WITH recent AS (
                SELECT id, commit_sha, COALESCE(started_at, created_at) AS run_at
                FROM runs WHERE repo_id = :repo_id AND changed_files_known
                ORDER BY run_at DESC, id DESC
                LIMIT :n
            )
            SELECT recent.id, recent.commit_sha, array_agg(DISTINCT tr.test_id)
            FROM recent JOIN test_results tr ON tr.run_id = recent.id
            WHERE tr.status IN ('failed', 'error')
            GROUP BY recent.id, recent.commit_sha, recent.run_at
            ORDER BY recent.run_at DESC, recent.id DESC
            """
        ),
        {**params, "n": config.co_change_runs},
    ).all()
    changed: dict[int, set[str]] = {}
    if failed:
        for run_id, path in session.execute(
            text("SELECT run_id, path FROM changed_files WHERE run_id = ANY(:ids)"),
            {"ids": [row[0] for row in failed]},
        ).all():
            changed.setdefault(run_id, set()).add(path)

    return RepoHistory(
        tests={test_id: KnownTest(test_id, file_path) for test_id, file_path in known},
        broken_on_main=dict(broken),
        recent_main_failures={test_id: (run_id, sha) for test_id, run_id, sha in recent_failures},
        failed_runs=tuple(
            FailedRun(run_id, sha, frozenset(changed.get(run_id, ())), frozenset(test_ids))
            for run_id, sha, test_ids in failed
        ),
    )


def select_for_repo(
    session: Session,
    repo: str,
    changed_files: Iterable[str],
    changed_files_known: bool,
    config: SelectorConfig | None = None,
) -> Selection:
    return select_tests(
        load_history(session, repo, config), changed_files, changed_files_known, config
    )
