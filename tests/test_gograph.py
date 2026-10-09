import subprocess
import sys
from pathlib import Path

import pytest

from siftwise.cli.gograph import GoGraph, build_graph, parse_go_list, run_go_list
from tests.gofixture import M, go_list_output, go_list_packages

A, B, C, D = (f"{M}/{x}" for x in "abcd")


@pytest.fixture
def graph(tmp_path: Path) -> GoGraph:
    return build_graph(go_list_packages(tmp_path), tmp_path)


def test_parse_concatenated_json_objects(tmp_path: Path) -> None:
    packages = parse_go_list(go_list_output(tmp_path))
    assert [p["ImportPath"] for p in packages][:3] == ["fmt", "github.com/x/y", M]
    assert parse_go_list("  \n") == []
    with pytest.raises(ValueError):
        parse_go_list('{"ImportPath": "a"} {broken')


def test_graph_maps_dirs_to_main_module_packages_only(graph: GoGraph) -> None:
    assert graph.module == M
    # No std/third-party packages, no test variants or generated test mains.
    assert dict(graph.dirs) == {
        "": M,
        "a": A,
        "b": B,
        "c": C,
        "d": D,
        "notests": f"{M}/notests",
    }


def test_graph_deps_include_test_imports(graph: GoGraph) -> None:
    assert graph.deps[A] == {A}
    assert graph.deps[B] == {A, B}
    assert graph.deps[C] == {A, B, C, D}  # d comes from the external c_test package


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["a/a.go"], {A: [A], B: [A], C: [A], f"{M}/notests": [A]}),
        (["d/d.go"], {C: [D], D: [D]}),  # c's tests import d
        (["b/b.go", "d/d.go"], {B: [B], C: [B, D], D: [D]}),
        (["version.go"], {M: [M]}),
        (["a/a_test.go"], {}),  # test files can't be imported by other packages
        (["vendor/x/x.go", "README.md", "a/data.json"], {}),
    ],
    ids=["a", "test-only-import", "two-packages", "root", "test-file", "not-go-packages"],
)
def test_affected_packages(
    graph: GoGraph, changed: list[str], expected: dict[str, list[str]]
) -> None:
    assert graph.affected(changed) == expected


def test_json_round_trip(graph: GoGraph) -> None:
    assert GoGraph.from_json(graph.to_json()) == graph


def test_run_go_list_returns_none_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Missing binary, non-zero exit (python can't open a file named "list") and timeout.
    assert run_go_list(tmp_path, go=str(tmp_path / "no-such-go")) is None
    assert run_go_list(tmp_path, go=sys.executable) is None

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("go", 1)

    monkeypatch.setattr("siftwise.cli.gograph.subprocess.run", timeout)
    assert run_go_list(tmp_path) is None


def test_run_go_list_parses_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, go_list_output(tmp_path).encode(), b"")

    monkeypatch.setattr("siftwise.cli.gograph.subprocess.run", fake_run)
    packages = run_go_list(tmp_path)

    assert calls == [["go", "list", "-deps", "-test", "-json", "./..."]]
    assert packages is not None and len(packages) == len(go_list_packages(tmp_path))


def test_run_go_list_rejects_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "siftwise.cli.gograph.subprocess.run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, b"{not json", b""),
    )
    assert run_go_list(tmp_path) is None
