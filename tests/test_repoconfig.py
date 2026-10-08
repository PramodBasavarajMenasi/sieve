from pathlib import Path

import pytest

from sieve.cli.repoconfig import (
    RepoConfig,
    RepoConfigError,
    load_repo_config,
    parse_repo_config,
    read_go_module,
)
from sieve.core.selector import DependsRule

KUBO_CONFIG = """
always_run = ["test/cli/smoke/**"]

[[depends]]
tests = "test/cli/**"
on = ["**/*.go", "!**/*_test.go"]

[[depends]]
tests = "tests/integration/**"
on = "src/**"
"""


def test_load_repo_config(tmp_path: Path) -> None:
    path = tmp_path / ".sieve.toml"
    path.write_text(KUBO_CONFIG)

    assert load_repo_config(path) == RepoConfig(
        depends=(
            DependsRule("test/cli/**", ("**/*.go", "!**/*_test.go")),
            DependsRule("tests/integration/**", ("src/**",)),  # a single string is allowed
        ),
        always_run=("test/cli/smoke/**",),
    )


def test_empty_config() -> None:
    assert parse_repo_config({}) == RepoConfig()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"depend": []}, "unknown key(s): depend"),
        ({"always_run": "x"}, "always_run must be a list of strings"),
        ({"depends": {"tests": "x"}}, "depends must be an array of tables"),
        ({"depends": ["x"]}, "#1 must be a table"),
        ({"depends": [{"tests": "x", "on": ["a"], "when": 1}]}, "only 'tests' and 'on'"),
        ({"depends": [{"on": ["a"]}]}, "'tests' must be a non-empty string"),
        ({"depends": [{"tests": "x", "on": []}]}, "needs at least one non-'!' glob"),
        ({"depends": [{"tests": "x", "on": ["!**/*_test.go"]}]}, "needs at least one non-'!'"),
    ],
)
def test_invalid_config(data: dict[str, object], message: str) -> None:
    with pytest.raises(RepoConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_repo_config(data)


def test_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / ".sieve.toml"
    path.write_text("[[depends]\n")
    with pytest.raises(RepoConfigError, match=r"\.sieve\.toml"):
        load_repo_config(path)


@pytest.mark.parametrize(
    ("go_mod", "expected"),
    [
        ("module github.com/ipfs/kubo\n\ngo 1.24\n", "github.com/ipfs/kubo"),
        ('// comment\nmodule "example.com/m" // trailing\n', "example.com/m"),
        ("go 1.24\n", None),
    ],
)
def test_read_go_module(tmp_path: Path, go_mod: str, expected: str | None) -> None:
    (tmp_path / "go.mod").write_text(go_mod)
    assert read_go_module(tmp_path) == expected


def test_read_go_module_without_go_mod(tmp_path: Path) -> None:
    assert read_go_module(tmp_path) is None
