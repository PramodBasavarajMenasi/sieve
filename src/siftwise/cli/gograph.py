"""Go import graph from ``go list -deps -test -json ./...``, for selecting dependent tests.

For every package of the main module, ``GoGraph.deps`` holds the main-module packages its
test binary is built from: the package itself plus everything its tests (in-package and
``_test`` package) import, transitively. ``go list`` reports ``Deps`` transitively already,
so a package is affected by a change exactly when its deps include a changed package.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Package = dict[str, Any]


@dataclass(frozen=True)
class GoGraph:
    module: str | None
    # repo-relative package directory ("" for the module root) -> import path
    dirs: Mapping[str, str]
    # testable import path -> main-module import paths its test build depends on
    deps: Mapping[str, frozenset[str]]

    def changed_packages(self, changed_files: Iterable[str]) -> set[str]:
        """Packages with changed non-test ``.go`` files (test files can't be imported)."""
        return {
            self.dirs[posixpath.dirname(path)]
            for path in changed_files
            if path.endswith(".go")
            and not path.endswith("_test.go")
            and posixpath.dirname(path) in self.dirs
        }

    def affected(self, changed_files: Iterable[str]) -> dict[str, list[str]]:
        """Package -> the changed packages its tests depend on, for every affected package."""
        changed = self.changed_packages(changed_files)
        if not changed:
            return {}
        return {
            package: sorted(deps & changed)
            for package, deps in sorted(self.deps.items())
            if deps & changed
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "dirs": dict(self.dirs),
            "deps": {pkg: sorted(deps) for pkg, deps in self.deps.items()},
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> GoGraph:
        return cls(
            module=data["module"],
            dirs=dict(data["dirs"]),
            deps={pkg: frozenset(deps) for pkg, deps in data["deps"].items()},
        )


def run_go_list(root: Path, go: str = "go", timeout: float = 900) -> list[Package] | None:
    """``go list -deps -test -json ./...`` in ``root``, or None if it fails for any reason."""
    try:
        proc = subprocess.run(
            [go, "list", "-deps", "-test", "-json", "./..."],
            cwd=root,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return parse_go_list(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def parse_go_list(output: str) -> list[Package]:
    """``go list -json`` prints concatenated JSON objects, not an array."""
    decoder = json.JSONDecoder()
    packages: list[Package] = []
    i, n = 0, len(output)
    while True:
        while i < n and output[i].isspace():
            i += 1
        if i >= n:
            return packages
        obj, i = decoder.raw_decode(output, i)
        if isinstance(obj, dict):
            packages.append(obj)


def build_graph(packages: Iterable[Package], root: Path) -> GoGraph:
    main = [p for p in packages if (p.get("Module") or {}).get("Main")]
    module = next(((p.get("Module") or {}).get("Path") for p in main), None)

    dirs: dict[str, str] = {}
    for p in main:
        path = p.get("ImportPath", "")
        if p.get("ForTest") or " " in path or _is_test_main(p):
            continue  # test variants share their package's directory
        rel = _relative_dir(p.get("Dir"), root)
        if rel is not None:
            dirs[rel] = path
    in_module = set(dirs.values())

    deps: dict[str, set[str]] = {}
    for p in main:
        if _is_test_main(p):
            continue
        base = p.get("ForTest") or p.get("ImportPath", "").split(" ", 1)[0]
        if base not in in_module:
            continue
        found = {d.split(" ", 1)[0] for d in p.get("Deps") or ()} | {base}
        deps.setdefault(base, set()).update(found & in_module)
    return GoGraph(module, dirs, {pkg: frozenset(d) for pkg, d in deps.items()})


def _is_test_main(p: Package) -> bool:
    # The generated "pkg.test" main package that drives a test binary.
    return p.get("Name") == "main" and str(p.get("ImportPath", "")).endswith(".test")


def _relative_dir(directory: str | None, root: Path) -> str | None:
    if not directory:
        return None
    try:
        rel = os.path.relpath(directory, root)
    except ValueError:  # different drive on Windows
        return None
    rel = Path(rel).as_posix()
    if rel.startswith(".."):
        return None
    return "" if rel == "." else rel
