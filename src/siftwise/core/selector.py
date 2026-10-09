"""Test selection: which tests to run for a change.

``select_tests`` is pure: given a repo's ``RepoHistory`` and a change, it returns a
``Selection``. ``load_history`` builds that history from the database, and ``select_for_repo``
does both.

Missing a real failure is worse than running extra tests, so whenever the selector is unsure
it returns ``Mode.FULL`` with a reason. It falls back to the full suite when:

* the changed files are unknown, or the list is empty;
* a build, dependency, CI or test-config file changed. A project manifest or lockfile in a
  subdirectory (``go.mod``, ``package.json``, ``pyproject.toml``, ...) is the exception: it
  belongs to a nested project, so it selects the tests under its own directory instead;
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
  - A changed or added Python test file runs whole (``pytest path/test_x.py``), so tests new
    in it run too. A deleted one runs nothing.
  - pytest collection errors (``pytest::a.b.c``: a module that failed to import) map to the
    module's file ``a/b/c.py``, which pytest then runs whole; doctests
    (``a.b::a.b.Thing``) to ``pytest --doctest-modules a/b.py``. If no file can be derived,
    the test is still selected but left out of the command rather than forcing the full suite.
* **Python imports**: test files that import a changed module (``affected_files``, from the
  CLI's static import graph) run whole.
* **Changed modules under --doctest-modules**: when history shows the repo's pytest collects
  source modules (doctests, or collection errors in non-test modules), changed and added
  ``.py`` modules run with ``pytest --doctest-modules``, so an import error in a new module
  or a broken doctest is caught.
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
import functools
import posixpath
import re
import shlex
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

from siftwise.core.junit import normalize_path


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

# Project manifests and their lockfiles. At the repo root they force the full suite; in a
# subdirectory they belong to a nested project (an example module, a sub-package) and only
# affect tests under that directory.
_SCOPED_BUILD_BASENAMES = frozenset(
    {
        "go.mod", "go.sum", "go.work", "go.work.sum",
        "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
        "pnpm-lock.yaml", "bun.lock", "bun.lockb",
        "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "uv.lock", "pdm.lock",
        "Pipfile", "Pipfile.lock",
    }
)  # fmt: skip
_SCOPED_BUILD_GLOBS = ("requirements*.txt",)

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
    # The run windows below count CI runs (repo, ci_run_id, run_attempt), not uploads: a CI
    # run with a 20-leg matrix (20 variant uploads) counts once.
    recent_main_runs: int = 10
    # How many recent CI runs to search for co-change failures.
    co_change_runs: int = 500
    # A test is "known" only if it appeared in one of the repo's latest N CI runs, so deleted
    # or renamed tests are never selected (pytest errors on node IDs that no longer exist).
    known_test_runs: int = 50
    # Go module path (e.g. "github.com/ipfs/kubo"). When set, Go commands use ./relative
    # package dirs and root-package changes can be mapped; otherwise import paths are used.
    go_module: str | None = None
    # Declared dependencies from the repo's .siftwise.toml.
    depends: tuple[DependsRule, ...] = ()


@dataclass(frozen=True)
class DependsRule:
    """``[[depends]]`` in .siftwise.toml: tests matching ``tests`` depend on files matching ``on``.

    Globs support ``*`` (within a path segment), ``**`` (any number of segments) and ``?``.
    Patterns in ``on`` starting with ``!`` exclude files. ``tests`` is matched against a
    test's file path, or for Go tests ``<package dir>/<TestName>``; it may match a suffix of
    the Go import path, so ``test/cli/**`` matches ``github.com/ipfs/kubo/test/cli`` tests.
    """

    tests: str
    on: tuple[str, ...]

    def matches_file(self, path: str) -> bool:
        include = [p for p in self.on if not p.startswith("!")]
        exclude = [p[1:] for p in self.on if p.startswith("!")]
        return any(glob_match(path, p) for p in include) and not any(
            glob_match(path, p) for p in exclude
        )


@functools.lru_cache(maxsize=1024)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    """Path glob: ``*`` and ``?`` stay within one segment, ``**`` spans segments."""
    return _glob_regex(pattern).match(path) is not None


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
    # Python files passed to pytest as a whole file: changed/added test files, test files that
    # import a changed module, collection errors and doctest modules. New tests in them run too.
    python_files_run_whole: tuple[str, ...] = ()

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
    affected_packages: Mapping[str, Sequence[str]] | None = None,
    affected_files: Mapping[str, Sequence[str]] | None = None,
    deleted_files: Iterable[str] = (),
) -> Selection:
    """Select tests for a change.

    ``affected_packages`` maps a Go package import path to the changed packages it imports
    (directly or transitively), as computed by the CLI from ``go list -deps -test``.
    ``affected_files`` maps a Python test file to the changed modules it imports (directly,
    transitively, via parent packages or conftest.py), from the CLI's static import graph.
    ``deleted_files`` are changed files that no longer exist: no command names them.
    """
    config = config or SelectorConfig()
    affected_packages = affected_packages or {}
    affected_files = {
        path: modules
        for file, modules in (affected_files or {}).items()
        if (path := normalize_path(file))
    }
    deleted = frozenset(p for p in (normalize_path(f) for f in deleted_files) if p)
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
    scoped_build = {p: d for p in paths if (d := build_file_scope(p)) is not None}
    build = [p for p in paths if is_build_file(p) and p not in scoped_build]
    if build:
        return full(f"build/config file changed: {_list(build)}")
    if history is None or not tests:
        return full("no test history for this repo")

    selected: dict[str, list[str]] = {}

    def add(test_id: str, reason: str) -> None:
        reasons = selected.setdefault(test_id, [])
        if reason not in reasons:
            reasons.append(reason)

    # Python test files, by path: the files pytest can be pointed at whole. (Doctests and
    # collection errors point at their module, which may be a source file.)
    py_test_files: dict[str, list[_Test]] = {}
    for test in tests.values():
        if (
            test.runner is Runner.PYTEST
            and test.py_path
            and not test.doctest
            and test.classname != PYTEST_COLLECTION_CLASSNAME
        ):
            py_test_files.setdefault(test.py_path, []).append(test)

    # Path mapping. Every changed source file must map to at least one test.
    relevant = [p for p in paths if not is_ignorable(p) and p not in scoped_build]
    unmapped = []
    whole_packages: set[str] = set()
    py_whole_files: set[str] = set()
    for path in relevant:
        is_py_test_file = path.endswith(".py") and (
            is_python_test_file(path) or path in py_test_files
        )
        if is_py_test_file and path in deleted:
            continue  # a deleted test file: its tests are gone, there's nothing to run
        hits = _map_path(path, tests, config)
        if is_py_test_file:
            # Changed or added test file: run it whole, so tests new in it run too.
            py_whole_files.add(path)
        elif not hits:
            unmapped.append(path)
        for test_id, reason in hits.items():
            add(test_id, reason)
            if path.endswith(".go") and tests[test_id].runner is Runner.GO:
                whole_packages.add(tests[test_id].classname)

    # A repo whose history has doctests or collection errors in source modules runs pytest with
    # --doctest-modules: pytest imports every module, so a changed or added module can fail at
    # import (a "pytest::a.b" collection error) or in its doctests. Run those modules too.
    py_module_files: set[str] = set()
    if any(t.collects_source_module for t in tests.values()):
        py_module_files = {
            p
            for p in relevant
            if p.endswith(".py") and p not in deleted and p not in py_whole_files
        }

    # Python dependents: test files that import a changed module. They run whole.
    covered_by_imports: set[str] = set()  # changed modules imported by a known test file
    for file, modules in sorted(affected_files.items()):
        known = py_test_files.get(file)
        if not known or file in deleted:
            continue
        first = sorted(modules)[0] if modules else "?"
        more = f" (+{len(modules) - 1} more)" if len(modules) > 1 else ""
        for test in known:
            add(test.test_id, f"imports changed module {first}{more}")
        py_whole_files.add(file)
        covered_by_imports.update(modules)

    # Nested manifests/lockfiles: every known test under their directory runs (Go packages
    # whole). They never force the full suite, even when no known tests live there.
    for path, directory in sorted(scoped_build.items()):
        for test in tests.values():
            if _test_under_dir(test, directory):
                add(test.test_id, f"build file {path} changed (tests under {directory}/)")
                if test.runner is Runner.GO:
                    whole_packages.add(test.classname)

    # Go dependents: packages whose tests import a changed package. They run whole.
    covered_by_dependents: set[str] = set()  # changed packages imported by a tested package
    for test in tests.values():
        imported = affected_packages.get(test.classname) if test.runner is Runner.GO else None
        if imported:
            first = sorted(imported)[0]
            more = f" (+{len(imported) - 1} more)" if len(imported) > 1 else ""
            add(test.test_id, f"imports changed package {first}{more}")
            whole_packages.add(test.classname)
            covered_by_dependents.update(imported)

    # Declared dependencies (.siftwise.toml [[depends]]).
    covered_by_rules: set[str] = set()
    for rule in config.depends:
        triggers = [p for p in relevant if rule.matches_file(p)]
        if not triggers:
            continue
        matched = [t for t in tests.values() if _test_matches_glob(t, rule.tests, config)]
        if not matched:
            continue
        covered_by_rules.update(triggers)
        for test in matched:
            add(test.test_id, f"declared dependency: {rule.tests} on {_list(triggers, 1)}")
            if test.runner is Runner.GO:
                whole_packages.add(test.classname)

    # A changed file is covered if its own tests, its package's dependents' tests, test files
    # importing it or a declared rule selected something; anything else forces the full suite.
    unmapped = [
        p
        for p in unmapped
        if p not in covered_by_rules
        and p not in covered_by_imports
        and not (
            p.endswith(".go")
            and any(
                _go_package_matches(pkg, posixpath.dirname(p), config.go_module)
                for pkg in covered_by_dependents
            )
        )
    ]
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

    # Tests in a deleted Python file no longer exist (pytest errors on a missing path).
    chosen = [
        tests[test_id]
        for test_id in sorted(selected)
        if not (tests[test_id].runner is Runner.PYTEST and tests[test_id].py_path in deleted)
    ]
    unrunnable = [t.test_id for t in chosen if t.runner is None]
    if unrunnable:
        return full(f"no known test runner for selected test(s): {_list(unrunnable)}")

    files_run_whole = (
        py_whole_files
        | py_module_files
        | {
            node
            for t in chosen
            if t.runner is Runner.PYTEST and (node := t.py_node) and "::" not in node
        }
    )
    if chosen:
        summary = f"{len(chosen)} of {len(tests)} known tests selected"
    elif py_whole_files:
        summary = f"{len(py_whole_files)} changed test file(s) run whole, no known tests"
    elif py_module_files:
        summary = f"{len(py_module_files)} changed module(s) run with --doctest-modules"
    else:
        summary = NO_TESTS_AFFECTED
    return Selection(
        mode=Mode.SELECTIVE,
        reason=summary,
        tests=tuple(
            SelectedTest(t.test_id, tuple(selected[t.test_id]), t.runner, t.file_path)
            for t in chosen
        ),
        selected_count=len(chosen),
        total_known=len(tests),
        commands=_build_commands(
            chosen, config.go_module, whole_packages, py_whole_files, py_module_files
        ),
        go_packages_run_whole=tuple(sorted(whole_packages)),
        python_files_run_whole=tuple(sorted(files_run_whole)),
    )


def is_build_file(path: str) -> bool:
    base = posixpath.basename(path)
    return (
        base in _BUILD_BASENAMES
        or any(fnmatch.fnmatchcase(base, glob) for glob in _BUILD_GLOBS)
        or path.startswith(_BUILD_DIRS)
    )


def build_file_scope(path: str) -> str | None:
    """The directory a nested project manifest or lockfile is scoped to.

    None for everything else, including root manifests: those affect the whole repo.
    """
    directory, base = posixpath.split(path)
    if directory and (
        base in _SCOPED_BUILD_BASENAMES
        or any(fnmatch.fnmatchcase(base, glob) for glob in _SCOPED_BUILD_GLOBS)
    ):
        return directory
    return None


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
    # Python only: what to pass to pytest (``path::Class::test`` or a whole file). None for a
    # collection error or doctest whose file can't be derived: selected, but left out of
    # commands.
    py_node: str | None = None
    # A doctest (run with ``pytest --doctest-modules <file>``).
    doctest: bool = False

    @property
    def collects_source_module(self) -> bool:
        """A doctest, or a collection error in a non-test module: evidence that the repo's
        pytest imports source modules (``--doctest-modules``)."""
        if self.runner is not Runner.PYTEST or not self.py_path:
            return False
        return self.doctest or (
            self.classname == PYTEST_COLLECTION_CLASSNAME and not is_python_test_file(self.py_path)
        )


def _index(known: Iterable[KnownTest]) -> dict[str, _Test]:
    return {k.test_id: _classify(k) for k in known}


# pytest reports a module that fails to import or collect as a testcase with no classname in
# a suite named "pytest", and the module's dotted name as the test name, so its test ID is
# e.g. "pytest::rdflib.plugins.serializers.n3" (or "pytest::test.test_foo").
PYTEST_COLLECTION_CLASSNAME = "pytest"


def _classify(test: KnownTest) -> _Test:
    classname, _, name = test.test_id.partition("::")
    file_path = normalize_path(test.file_path)
    ext = posixpath.splitext(file_path or "")[1]

    if classname == PYTEST_COLLECTION_CLASSNAME:
        module = _collection_error_path(name, file_path)
        return _Test(test.test_id, classname, name, file_path, Runner.PYTEST, module, (), module)
    if _is_doctest(classname, name, ext):
        module = _collection_error_path(classname, file_path)
        return _Test(
            test.test_id, classname, name, file_path, Runner.PYTEST, module, (), module, True
        )

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
        py_path, py_classes = python
        node = "::".join((py_path, *py_classes, name))
        return _Test(test.test_id, classname, name, file_path, runner, py_path, py_classes, node)
    return _Test(test.test_id, classname, name, file_path, runner)


def _is_doctest(classname: str, name: str, ext: str) -> bool:
    """pytest ``--doctest-modules`` items: the classname is the module (``rdflib.container``)
    and the name is the documented object's dotted path inside it
    (``rdflib.container.Container``), or the module itself for its module docstring."""
    if not classname or ext not in ("", ".py") or any(c.isspace() or c == "/" for c in classname):
        return False
    return name.startswith(classname + ".") or (name == classname and "." in classname)


def is_python_test_file(path: str) -> bool:
    """pytest's default ``python_files``: ``test_*.py`` or ``*_test.py``."""
    base = posixpath.basename(path)
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def pytest_file(test_id: str, file_path: str | None = None) -> str | None:
    """The file pytest runs ``test_id`` from (test module, doctest module or the module of a
    collection error), or None if it isn't a pytest test or no file can be derived."""
    test = _classify(KnownTest(test_id, file_path))
    return test.py_path if test.runner is Runner.PYTEST else None


def _collection_error_path(module: str, file_path: str | None) -> str | None:
    """The file of a dotted module name: ``a.b.c`` -> ``a/b/c.py`` (or its file_path).

    None if the name isn't a dotted module name, so no file can be derived.
    """
    if file_path and file_path.endswith(".py"):
        return file_path
    parts = module.split(".")
    if not module or not all(_PY_IDENTIFIER.match(p) for p in parts):
        return None
    return "/".join(parts) + ".py"


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


def _test_under_dir(test: _Test, directory: str) -> bool:
    prefix = directory + "/"
    if any(path and path.startswith(prefix) for path in (test.file_path, test.py_path)):
        return True
    # Go: the package's import path contains the directory, e.g. a nested module
    # github.com/ipfs/kubo/docs/examples/kubo-as-a-library for docs/examples/kubo-as-a-library.
    return test.runner is Runner.GO and f"/{directory}/" in f"/{test.classname}/"


def _test_matches_glob(test: _Test, pattern: str, config: SelectorConfig) -> bool:
    """Whether a declared-dependency ``tests`` glob covers ``test``."""
    if any(path and glob_match(path, pattern) for path in (test.file_path, test.py_path)):
        return True
    if test.runner is Runner.GO:
        # "<import path>/<TestName>", matched at any path-segment suffix so that
        # "test/cli/**" works with or without go_module: .../kubo/test/cli/TestAdd.
        parts = f"{test.classname}/{test.name.split('/', 1)[0]}".split("/")
        return any(glob_match("/".join(parts[i:]), pattern) for i in range(len(parts)))
    return False


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
    py_whole_files: Collection[str] = (),
    py_module_files: Collection[str] = (),
) -> tuple[str, ...]:
    """One command per runner (per package for Go), in a stable order.

    Go packages in ``go_whole_packages`` run without -run, so tests that history doesn't
    know yet (added in this change) run too. Other Go packages only had tests pulled in by
    co-change, recently-failed or always-run, so they keep -run for just those tests.
    Python files in ``py_whole_files`` are likewise passed to pytest whole.
    """
    commands: list[str] = []

    nodes = {t.py_node for t in tests if t.runner is Runner.PYTEST and t.py_node and not t.doctest}
    nodes.update(py_whole_files)
    # A whole file (changed, imports a changed module, or a collection error) already runs
    # every test in it.
    whole_files = {node for node in nodes if "::" not in node}
    pytest_ids = sorted(
        node for node in nodes if "::" not in node or node.partition("::")[0] not in whole_files
    )
    if pytest_ids:
        commands.append(shlex.join(["pytest", *pytest_ids]))
    known_doctest_files = {t.py_node for t in tests if t.doctest and t.py_node}
    doctest_files = sorted(known_doctest_files | set(py_module_files))
    if doctest_files:
        command = shlex.join(["pytest", "--doctest-modules", *doctest_files])
        if set(doctest_files) - known_doctest_files:
            # A module may have no doctests: pytest then exits 5 ("no tests collected"),
            # which isn't a failure. Import errors (exit 2) and doctest failures (1) still are.
            command = f"{{ {command} || test $? -eq 5; }}"
        commands.append(command)

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


# The repo's runs, with the columns recent_ci_runs_sql() expects.
_REPO_RUNS = """
repo_runs AS (
    SELECT id, ci_run_id, run_attempt, commit_sha, is_main, changed_files_known,
           COALESCE(started_at, created_at) AS run_at
    FROM runs WHERE repo_id = :repo_id
)
"""


def recent_ci_runs_sql(source: str, condition: str = "true") -> str:
    """CTEs ``recent_ci`` and ``recent``: every run of the latest ``:n`` *CI runs* in ``source``.

    A CI run is ``(ci_run_id, run_attempt)``: all its matrix variants (one upload each) count
    once, so "the last 50 runs" means 50 CI runs whether the matrix has 1 leg or 20. Runs
    without a ``ci_run_id`` count on their own. ``source`` must have ``id, ci_run_id,
    run_attempt, commit_sha, run_at``; ``condition`` filters it (e.g. ``is_main``).
    ``recent`` has ``id, commit_sha, run_at``. ``condition`` may only use ``source`` columns
    that ``recent_ci`` doesn't have (e.g. ``is_main``), so they need no qualifier.
    """
    return f"""
recent_ci AS (
    SELECT COALESCE(ci_run_id, 'run:' || id) AS ci_key, run_attempt,
           max(run_at) AS ci_at, max(id) AS ci_id
    FROM {source} WHERE {condition}
    GROUP BY 1, 2
    ORDER BY ci_at DESC, ci_id DESC
    LIMIT :n
),
recent AS (
    SELECT s.id, s.commit_sha, s.run_at
    FROM {source} s
    JOIN recent_ci c
      ON COALESCE(s.ci_run_id, 'run:' || s.id) = c.ci_key AND s.run_attempt = c.run_attempt
    WHERE {condition}
)
"""


def load_history(
    session: Session, repo: str, config: SelectorConfig | None = None
) -> RepoHistory | None:
    """The selector's view of ``repo``'s history, or None if the repo is unknown.

    Never reads passing raw results, so its cost doesn't grow with the size of the test suite
    or the matrix: known tests come from ``test_stats`` (its last-seen run, kept by the
    rollup), and the failure queries go through the partial index on failed results. Known
    tests are therefore as fresh as the rollup (stale between a deferred batch ingest and
    ``POST /repos/{repo}/rollup``).
    """
    config = config or SelectorConfig()
    repo_id = session.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
    ).scalar_one_or_none()
    if repo_id is None:
        return None
    params = {"repo_id": repo_id}

    # Tests that appeared in one of the latest N CI runs: their last-seen run is in one of them
    # (core/history.py orders "last seen" by the same CI-run order as recent_ci_runs_sql).
    known = session.execute(
        text(
            f"""
            WITH {_REPO_RUNS}, {recent_ci_runs_sql("repo_runs")}
            SELECT ts.test_id, ts.file_path
            FROM test_stats ts JOIN recent ON recent.id = ts.last_seen_run_id
            WHERE ts.repo_id = :repo_id
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
            f"""
            WITH {_REPO_RUNS}, {recent_ci_runs_sql("repo_runs", "is_main")}
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
            f"""
            WITH {_REPO_RUNS}, {recent_ci_runs_sql("repo_runs", "changed_files_known")}
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
