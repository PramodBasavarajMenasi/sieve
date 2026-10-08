from pathlib import Path

import pytest

from scripts.eval.pygraph import PyGraph, build_py_graph, conftests_for, module_of_test

TREE = {
    "pkg/__init__.py": "from .core import thing\n",
    "pkg/core.py": "import pkg.util\nthing = 1\n",
    "pkg/util.py": "def helper(): ...\n",
    "pkg/sub/__init__.py": "",
    "pkg/sub/mod.py": "from ..util import helper\nfrom . import sibling\n",
    "pkg/sub/sibling.py": "",
    "pkg/unused.py": "",
    "tests/conftest.py": "import pkg.sub.mod\n",
    "tests/test_core.py": "from pkg import core\n",
    "tests/test_plain.py": "import os\n",
    "tests/unit/test_deep.py": "import json\n",
    "src/lib2/__init__.py": "",
    "src/lib2/a.py": "",
    "tests/test_lib2.py": "import lib2.a\n",
    "broken.py": "def oops(:\n",
    ".venv/site.py": "import pkg\n",
}


@pytest.fixture
def graph(tmp_path: Path) -> PyGraph:
    for name, content in TREE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return build_py_graph(tmp_path)


def test_absolute_imports_include_parent_packages(graph: PyGraph) -> None:
    assert graph.edges["tests/test_core.py"] == {"pkg/__init__.py", "pkg/core.py"}
    assert graph.edges["pkg/core.py"] == {"pkg/__init__.py", "pkg/util.py"}


def test_relative_imports(graph: PyGraph) -> None:
    # `from ..util import helper` and `from . import sibling` inside pkg/sub/mod.py.
    assert graph.edges["pkg/sub/mod.py"] == {
        "pkg/__init__.py",
        "pkg/util.py",
        "pkg/sub/__init__.py",
        "pkg/sub/sibling.py",
    }
    assert graph.edges["pkg/__init__.py"] == {"pkg/core.py"}  # `from .core import thing`


def test_src_layout_and_skipped_files(graph: PyGraph) -> None:
    assert graph.edges["tests/test_lib2.py"] == {"src/lib2/__init__.py", "src/lib2/a.py"}
    assert graph.edges["broken.py"] == frozenset()  # syntax error: no edges, no crash
    assert not any(f.startswith(".venv/") for f in graph.edges)


def test_import_chain_follows_imports(graph: PyGraph) -> None:
    chain = graph.import_chain("tests/test_core.py", {"pkg/util.py"})
    assert chain is not None
    assert chain[-1] == "pkg/util.py"
    assert len(chain) == 3  # a test file, one hop, the target


def test_import_chain_includes_conftest(graph: PyGraph) -> None:
    # test_plain imports nothing from the repo, but pytest loads tests/conftest.py for it.
    assert graph.import_chain("tests/test_plain.py", {"pkg/sub/sibling.py"}) == [
        "tests/conftest.py",
        "pkg/sub/mod.py",
        "pkg/sub/sibling.py",
    ]
    assert conftests_for("tests/unit/test_deep.py", graph.edges) == ["tests/conftest.py"]


def test_no_chain_to_an_unimported_module(graph: PyGraph) -> None:
    assert graph.import_chain("tests/test_core.py", {"pkg/unused.py"}) is None
    assert graph.import_chain("tests/missing.py", {"pkg/util.py"}) is None


def test_json_round_trip(graph: PyGraph) -> None:
    assert PyGraph.from_json(graph.to_json()) == graph


@pytest.mark.parametrize(
    ("test_id", "expected"),
    [
        ("test.test_graph.test_graph::test_x", "test/test_graph/test_graph.py"),
        ("test.test_graph.TestGraph::test_x", "test/test_graph.py"),
        ("tests.unit.graph_test.TestG::test_x", "tests/unit/graph_test.py"),
        ("com.acme.GraphTest::testX", None),  # not a pytest module
    ],
)
def test_module_of_test_id(test_id: str, expected: str | None) -> None:
    assert module_of_test(test_id) == expected
