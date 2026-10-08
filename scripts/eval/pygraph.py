"""Static Python import graph of a checkout, for explaining eval misses.

Used only by ``run_eval.py`` to answer "would a Python import graph have selected this
failing test?". The selector does not use it.

Edges are file -> files it imports (repo-relative paths). ``import a.b.c`` also loads ``a`` and
``a.b``, so those packages' ``__init__.py`` files are edges too. A test file is also treated
as importing every ``conftest.py`` in its directory and above, since pytest loads them.
"""

from __future__ import annotations

import ast
import posixpath
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", ".tox", ".nox", "build", "dist", "node_modules", ".eggs",
     "site-packages", "__pycache__", ".mypy_cache", ".pytest_cache"}
)  # fmt: skip


@dataclass(frozen=True)
class PyGraph:
    edges: Mapping[str, frozenset[str]]

    def import_chain(self, test_file: str, targets: set[str]) -> list[str] | None:
        """Shortest import chain from ``test_file`` (plus its conftests) to any target."""
        starts = [test_file, *conftests_for(test_file, self.edges)]
        parent: dict[str, str | None] = {s: None for s in starts if s in self.edges}
        queue = deque(parent)
        while queue:
            current = queue.popleft()
            if current in targets:
                chain = [current]
                while (previous := parent[chain[-1]]) is not None:
                    chain.append(previous)
                return chain[::-1]
            for nxt in self.edges.get(current, ()):
                if nxt not in parent:
                    parent[nxt] = current
                    queue.append(nxt)
        return None

    def to_json(self) -> dict[str, Any]:
        return {"edges": {f: sorted(e) for f, e in self.edges.items()}}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PyGraph:
        return cls({f: frozenset(e) for f, e in data["edges"].items()})


def conftests_for(test_file: str, edges: Mapping[str, Any]) -> list[str]:
    directory = posixpath.dirname(test_file)
    found = []
    while True:
        candidate = posixpath.join(directory, "conftest.py") if directory else "conftest.py"
        if candidate in edges and candidate != test_file:
            found.append(candidate)
        if not directory:
            return found
        directory = posixpath.dirname(directory)


def test_file_for(test_id: str) -> str | None:
    """A pytest test ID's module: ``test.test_graph.TestX::t`` -> ``test/test_graph.py``."""
    parts = test_id.partition("::")[0].split(".")
    modules = [i for i, p in enumerate(parts) if p.startswith("test_") or p.endswith("_test")]
    if not modules:
        return None
    return "/".join(parts[: modules[-1] + 1]) + ".py"


def build_py_graph(root: Path) -> PyGraph:
    files: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        files.append(rel.as_posix())

    modules: dict[str, str] = {}  # dotted module name -> file
    for file in files:
        for name in _module_names(file):
            modules.setdefault(name, file)

    edges: dict[str, frozenset[str]] = {}
    for file in files:
        try:
            tree = ast.parse((root / file).read_bytes(), filename=file)
        except (SyntaxError, ValueError):
            edges[file] = frozenset()
            continue
        targets: set[str] = set()
        package = _package_of(file)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    targets.update(_resolve(alias.name, modules))
            elif isinstance(node, ast.ImportFrom):
                base = _absolute(node.module, node.level, package)
                if base is None:
                    continue
                targets.update(_resolve(base, modules))
                for alias in node.names:  # `from pkg import submodule`
                    if alias.name != "*":
                        targets.update(_resolve(f"{base}.{alias.name}" if base else alias.name,
                                                modules, ancestors=False))  # fmt: skip
        targets.discard(file)
        edges[file] = frozenset(targets)
    return PyGraph(edges)


def _module_names(file: str) -> Iterable[str]:
    parts = file[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    names = [".".join(parts)]
    if parts[0] in ("src", "lib") and len(parts) > 1:  # src layout
        names.append(".".join(parts[1:]))
    return names


def _package_of(file: str) -> str:
    parts = file[: -len(".py")].split("/")
    package = parts[:-1]  # a module's package; __init__.py's package is its directory
    if parts[0] in ("src", "lib") and len(package) > 1:
        package = package[1:]
    return ".".join(package)


def _absolute(module: str | None, level: int, package: str) -> str | None:
    if level == 0:
        return module
    pieces = package.split(".") if package else []
    if level - 1 > len(pieces):
        return None
    base = pieces[: len(pieces) - (level - 1)]
    if module:
        base.append(module)
    return ".".join(base)


def _resolve(name: str, modules: Mapping[str, str], ancestors: bool = True) -> set[str]:
    """Files loaded by importing ``name``: the module and (optionally) its parent packages."""
    found: set[str] = set()
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        file = modules.get(".".join(parts[:i]))
        if file:
            found.add(file)
            if not ancestors:
                break
        elif not ancestors and i == len(parts):
            break
    return found
