import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from siftwise.cli.main import app, parse_deleted, parse_name_status
from tests.gofixture import M, go_list_packages

SERVER = "http://siftwise.test"
SELECT_URL = f"{SERVER}/select"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with `main` and a `feature` branch that modifies, adds, deletes, renames."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cart.py").write_text("def total():\n    return 1\n")
    (tmp_path / "src" / "old_name.py").write_text("".join(f"line {i}\n" for i in range(50)))
    (tmp_path / "src" / "gone.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("# shop\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")

    git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "src" / "cart.py").write_text("def total():\n    return 2\n")
    (tmp_path / "src" / "with space.py").write_text("y = 2\n")
    git(tmp_path, "mv", "src/old_name.py", "src/new_name.py")
    git(tmp_path, "rm", "-q", "src/gone.py")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "feature")

    # A commit on main after branching must not show up in main...feature.
    git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "README.md").write_text("# shop, updated on main\n")
    git(tmp_path, "commit", "-q", "-am", "main moves on")
    git(tmp_path, "checkout", "-q", "feature")

    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "cli-token")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def response(**overrides: Any) -> dict[str, Any]:
    return {
        "repo": "acme/shop",
        "mode": "selective",
        "reason": "2 of 10 known tests selected",
        "selected_count": 2,
        "total_known": 10,
        "command": "pytest tests/test_cart.py::test_total",
        "commands": ["pytest tests/test_cart.py::test_total"],
        "tests": [],
        **overrides,
    }


def run(*args: str) -> Result:
    return CliRunner().invoke(app, ["select", "--repo", "acme/shop", "--server", SERVER, *args])


def sent(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


# --- happy path ---------------------------------------------------------------------------


@pytest.mark.usefixtures("repo")
def test_prints_command_for_branch_diff(api: respx.MockRouter) -> None:
    route = api.post(SELECT_URL).respond(json=response())

    result = run()

    assert result.exit_code == 0, result.output
    assert result.stdout == "pytest tests/test_cart.py::test_total\n"
    assert (
        result.stderr == "siftwise: selective (2 of 10 known tests): 2 of 10 known tests selected\n"
    )
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer cli-token"
    assert sent(route) == {
        "repo": "acme/shop",
        # Renames give both paths; README.md changed only on main, so it's not included.
        "changed_files": [
            "src/cart.py",
            "src/gone.py",
            "src/old_name.py",  # rename: old path, then new
            "src/new_name.py",
            "src/with space.py",
        ],
        "changed_files_known": True,
        # No .siftwise.toml and no go.mod: empty rules, no go_module or affected_packages.
        "depends": [],
        "always_run": [],
        # The deleted file and the old side of the rename: never named in a command.
        "deleted_files": ["src/gone.py", "src/old_name.py"],
        # Python files changed, so the import graph ran; no test file imports them.
        "affected_files": {},
    }


def test_base_and_head_options(repo: Path, api: respx.MockRouter) -> None:
    route = api.post(SELECT_URL).respond(json=response())

    run("--base", "main~1", "--head", "main")

    assert sent(route)["changed_files"] == ["README.md"]


@pytest.mark.usefixtures("repo")
def test_json_output(api: respx.MockRouter) -> None:
    body = response(tests=[{"test_id": "t::a", "reasons": ["r"], "reason": "r",
                            "runner": "pytest", "file_path": None}])  # fmt: skip
    api.post(SELECT_URL).respond(json=body)

    result = run("--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == body


@pytest.mark.usefixtures("repo")
def test_no_tests_affected_prints_message_and_exits_0(api: respx.MockRouter) -> None:
    api.post(SELECT_URL).respond(
        json=response(reason="no tests affected", selected_count=0, command="", commands=[])
    )

    result = run()

    assert result.exit_code == 0
    assert result.stdout == ""  # `eval "$(siftwise select ...)"` runs nothing
    assert "no tests affected" in result.stderr


@pytest.mark.usefixtures("repo")
def test_full_mode_prints_full_command(api: respx.MockRouter) -> None:
    api.post(SELECT_URL).respond(
        json=response(mode="full", reason="build/config file changed: go.mod",
                      command="go test ./...", commands=["go test ./..."])
    )  # fmt: skip

    result = run()

    assert result.exit_code == 0
    assert result.stdout == "go test ./...\n"
    assert "siftwise: full" in result.stderr and "go.mod" in result.stderr


@pytest.mark.usefixtures("repo")
def test_full_mode_without_a_command_fails(api: respx.MockRouter) -> None:
    # e.g. a Java-only history: the full suite is needed but siftwise can't name the command.
    api.post(SELECT_URL).respond(json=response(mode="full", reason="x", command="", commands=[]))

    result = run()

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "full suite is needed" in result.stderr


# --- git failures -------------------------------------------------------------------------


def test_bad_ref_sends_changed_files_unknown(repo: Path, api: respx.MockRouter) -> None:
    route = api.post(SELECT_URL).respond(json=response(mode="full"))

    result = run("--base", "no-such-branch")

    assert result.exit_code == 0
    assert sent(route)["changed_files"] == []
    assert sent(route)["changed_files_known"] is False
    assert "git diff no-such-branch...HEAD failed" in result.stderr


def test_outside_a_git_repo_sends_changed_files_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: respx.MockRouter
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    route = api.post(SELECT_URL).respond(json=response(mode="full"))

    run()

    assert sent(route)["changed_files_known"] is False


def test_missing_git_sends_changed_files_unknown(
    repo: Path, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr("siftwise.cli.main.subprocess.run", no_git)
    route = api.post(SELECT_URL).respond(json=response(mode="full"))

    result = run()

    assert sent(route)["changed_files_known"] is False
    assert "could not run git diff" in result.stderr


# --- API and config errors ----------------------------------------------------------------


@pytest.mark.usefixtures("repo")
@pytest.mark.parametrize(
    ("api_response", "message"),
    [
        (httpx.Response(404, json={"detail": "unknown repo 'acme/shop'"}),
         "siftwise returned 404: unknown repo 'acme/shop'"),
        (httpx.Response(401, json={"detail": "invalid or missing bearer token"}),
         "siftwise returned 401: invalid or missing bearer token"),
        (httpx.Response(502, text="Bad Gateway"), "siftwise returned 502: Bad Gateway"),
        (httpx.ConnectError("refused"), f"could not reach siftwise at {SERVER}"),
    ],
    ids=["unknown-repo", "bad-token", "server-error", "unreachable"],
)  # fmt: skip
def test_api_errors_exit_1(
    api: respx.MockRouter, api_response: httpx.Response | Exception, message: str
) -> None:
    api.post(SELECT_URL).mock(side_effect=[api_response])

    result = run()

    assert result.exit_code == 1
    assert result.stdout == ""
    assert message in result.stderr


def test_missing_token_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIFTWISE_API_TOKEN")
    result = run()
    assert result.exit_code == 2
    assert "SIFTWISE_API_TOKEN" in result.stderr


# --- Go import graph and .siftwise.toml ------------------------------------------------------

SIFTWISE_TOML = """
always_run = ["smoke/**"]

[[depends]]
tests = "e2e/**"
on = ["**/*.go", "!**/*_test.go"]
"""


@pytest.fixture
def go_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Go module (see tests/gofixture.py) whose feature branch changes package a."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "go.mod").write_text(f"module {M}\n\ngo 1.24\n")
    (tmp_path / ".siftwise.toml").write_text(SIFTWISE_TOML)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "a.go").write_text("package a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "a" / "a.go").write_text("package a\n\nvar X = 1\n")
    git(tmp_path, "commit", "-q", "-am", "change a")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_go_dependents_and_repo_config_are_sent(
    go_repo: Path, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots: list[Path] = []

    def fake_go_list(root: Path) -> list[dict[str, Any]]:
        roots.append(root)
        return go_list_packages(root)

    monkeypatch.setattr("siftwise.cli.main.run_go_list", fake_go_list)
    route = api.post(SELECT_URL).respond(json=response())

    result = run()

    assert result.exit_code == 0, result.output
    assert roots[0].resolve() == go_repo.resolve()  # go list runs at the repo root
    body = sent(route)
    assert body["go_module"] == M
    assert body["affected_packages"] == {
        f"{M}/a": [f"{M}/a"],
        f"{M}/b": [f"{M}/a"],
        f"{M}/c": [f"{M}/a"],
        f"{M}/notests": [f"{M}/a"],
    }
    assert body["depends"] == [{"tests": "e2e/**", "on": ["**/*.go", "!**/*_test.go"]}]
    assert body["always_run"] == ["smoke/**"]
    assert "4 package(s) depend on changed Go code" in result.stderr


@pytest.mark.usefixtures("go_repo")
def test_go_list_failure_sends_no_dependents(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("siftwise.cli.main.run_go_list", lambda root: None)
    route = api.post(SELECT_URL).respond(json=response())

    result = run()

    assert result.exit_code == 0
    assert sent(route)["affected_packages"] == {}  # existing fallbacks decide
    assert "go list -deps -test -json ./...` failed" in result.stderr


@pytest.mark.usefixtures("go_repo")
def test_no_go_list_option(api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(root: Path) -> None:
        raise AssertionError("go list should not run")

    monkeypatch.setattr("siftwise.cli.main.run_go_list", must_not_run)
    route = api.post(SELECT_URL).respond(json=response())

    run("--no-go-list")

    assert "affected_packages" not in sent(route)
    assert sent(route)["go_module"] == M


def test_go_list_skipped_when_diff_unknown(
    go_repo: Path, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("siftwise.cli.main.run_go_list", lambda root: pytest.fail("ran go list"))
    route = api.post(SELECT_URL).respond(json=response(mode="full"))

    run("--base", "no-such-branch")

    assert sent(route)["changed_files_known"] is False
    assert "affected_packages" not in sent(route)


@pytest.mark.usefixtures("go_repo")
def test_invalid_siftwise_toml_exits_2(api: respx.MockRouter) -> None:
    Path(".siftwise.toml").write_text('[[depends]]\ntests = "e2e/**"\non = []\n')
    route = api.post(SELECT_URL).respond(json=response())

    result = run()

    assert result.exit_code == 2
    assert "needs at least one non-'!' glob" in result.stderr
    assert not route.called


# --- parsing git output -------------------------------------------------------------------


def test_parse_name_status() -> None:
    output = (
        b"M\0src/a.py\0"
        b"A\0src/new file.py\0"
        b"D\0src/gone.py\0"
        b"R087\0src/old.py\0src/renamed.py\0"
        b"C100\0src/template.py\0src/copy.py\0"
        b"T\0src/link\0"
        b"M\0src/a.py\0"
    )
    assert parse_name_status(output) == [
        "src/a.py",
        "src/new file.py",
        "src/gone.py",
        "src/old.py",
        "src/renamed.py",
        "src/copy.py",  # copies: the source is unchanged
        "src/link",
    ]


def test_parse_empty_diff() -> None:
    assert parse_name_status(b"") == []


def test_parse_deleted() -> None:
    output = b"M\0a.py\0D\0gone.py\0R090\0old.py\0new.py\0C100\0t.py\0copy.py\0A\0n.py\0"
    assert parse_deleted(output) == ["gone.py", "old.py"]
    assert parse_deleted(b"") == []


# --- Python import graph ------------------------------------------------------------------


@pytest.fixture
def py_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """shop/money.py changes; tests/test_cart.py imports it through shop/cart.py."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    files = {
        "shop/__init__.py": "",
        "shop/money.py": "RATE = 1\n",
        "shop/cart.py": "from shop.money import RATE\n",
        "shop/tax.py": "",
        "tests/test_cart.py": "from shop import cart\n",
        "tests/test_tax.py": "import shop.tax\n",
    }
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "shop" / "money.py").write_text("RATE = 2\n")
    git(tmp_path, "commit", "-q", "-am", "change money")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.usefixtures("py_repo")
def test_python_importers_are_sent(api: respx.MockRouter) -> None:
    route = api.post(SELECT_URL).respond(json=response())

    result = run()

    assert result.exit_code == 0, result.output
    assert sent(route)["affected_files"] == {"tests/test_cart.py": ["shop/money.py"]}
    assert "deleted_files" not in sent(route)
    assert "python imports: 1 test file(s) import changed modules" in result.stderr


@pytest.mark.usefixtures("py_repo")
def test_python_graph_failure_sends_no_importers(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(root: Path) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr("siftwise.cli.main.build_py_graph", broken)
    route = api.post(SELECT_URL).respond(json=response())

    result = run()

    assert result.exit_code == 0
    assert sent(route)["affected_files"] == {}  # existing fallbacks decide
    assert "could not build the Python import graph (disk on fire)" in result.stderr


@pytest.mark.usefixtures("py_repo")
def test_no_py_imports_option(api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("siftwise.cli.main.build_py_graph", lambda root: pytest.fail("built graph"))
    route = api.post(SELECT_URL).respond(json=response())

    run("--no-py-imports")

    assert "affected_files" not in sent(route)


def test_python_graph_skipped_without_python_changes(
    py_repo: Path, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("siftwise.cli.main.build_py_graph", lambda root: pytest.fail("built graph"))
    route = api.post(SELECT_URL).respond(json=response())
    (py_repo / "README.md").write_text("docs\n")
    git(py_repo, "add", "-A")
    git(py_repo, "commit", "-q", "-m", "docs")

    run("--base", "HEAD~1")

    assert sent(route)["changed_files"] == ["README.md"]
    assert "affected_files" not in sent(route)
