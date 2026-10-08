from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from sieve.api.main import create_app
from sieve.config import Settings
from sieve.core.ingest import create_run
from sieve.core.junit import ParsedTestResult, Status
from sieve.core.schemas import RunMetadata
from sieve.db import get_session

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO = "acme/shop"
GO_PKG = "github.com/acme/shop/cart"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_session: Session) -> None:
    """Two passing runs of a small Python + Go repo."""
    tests = [
        "tests.test_cart::test_total",
        "tests.test_cart.TestDiscount::test_applies",
        "tests.test_db::test_query",
        f"{GO_PKG}::TestAdd",
        f"{GO_PKG}::TestAdd/with_discount",
    ]
    for n in (1, 2):
        results = [
            ParsedTestResult(
                test_id=test_id,
                classname=test_id.split("::")[0],
                name=test_id.split("::")[1],
                file_path=None,
                status=Status.PASSED,
                duration_ms=1,
                attempt=1,
            )
            for test_id in tests
        ]
        meta = RunMetadata(
            repo=REPO,
            commit_sha=f"{n:040x}",
            branch="main",
            is_main=True,
            started_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=n),
        )
        create_run(db_session, meta, results)


def post_select(
    client: TestClient, headers: dict[str, str] | None = None, **body: Any
) -> httpx.Response:
    response: httpx.Response = client.post(
        "/select", json={"repo": REPO, **body}, headers=AUTH if headers is None else headers
    )
    return response


@pytest.mark.usefixtures("seeded")
def test_selective_response(client: TestClient) -> None:
    response = post_select(client, changed_files=["src/shop/cart.py", "cart/total.go"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repo"] == REPO
    assert body["mode"] == "selective"
    assert body["reason"] == "4 of 5 known tests selected"
    assert (body["selected_count"], body["total_known"]) == (4, 5)
    assert body["commands"] == [
        "pytest tests/test_cart.py::TestDiscount::test_applies tests/test_cart.py::test_total",
        f"go test {GO_PKG} -run '^(TestAdd)$'",
    ]
    assert body["command"] == " && ".join(body["commands"])
    by_id = {t["test_id"]: t for t in body["tests"]}
    assert set(by_id) == {
        "tests.test_cart::test_total",
        "tests.test_cart.TestDiscount::test_applies",
        f"{GO_PKG}::TestAdd",
        f"{GO_PKG}::TestAdd/with_discount",
    }
    assert by_id["tests.test_cart::test_total"] == {
        "test_id": "tests.test_cart::test_total",
        "reasons": ["tests/test_cart.py tests changed src/shop/cart.py"],
        "reason": "tests/test_cart.py tests changed src/shop/cart.py",
        "runner": "pytest",
        "file_path": None,
    }
    assert by_id[f"{GO_PKG}::TestAdd"]["runner"] == "go"


@pytest.mark.usefixtures("seeded")
def test_docs_only_change_selects_nothing(client: TestClient) -> None:
    body = post_select(client, changed_files=["README.md", "docs/setup.md"]).json()

    assert body["mode"] == "selective"
    assert body["reason"] == "no tests affected"
    assert (body["selected_count"], body["total_known"]) == (0, 5)
    assert (body["command"], body["commands"], body["tests"]) == ("", [], [])


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"changed_files": ["src/shop/cart.py"], "changed_files_known": False},
         "changed files are unknown for this change"),
        ({}, "no changed files were given"),
        ({"changed_files": ["go.mod"]}, "build/config file changed: go.mod"),
        ({"changed_files": ["src/shop/util.py"]},
         "changed file maps to no known tests: src/shop/util.py"),
    ],
    ids=["unknown-diff", "empty", "build-file", "unmapped"],
)  # fmt: skip
def test_full_suite_fallbacks(client: TestClient, body: dict[str, Any], reason: str) -> None:
    result = post_select(client, **body).json()

    assert result["mode"] == "full"
    assert result["reason"] == reason
    assert (result["selected_count"], result["total_known"]) == (5, 5)
    assert result["command"] == "pytest && go test ./..."
    assert result["tests"] == []


@pytest.mark.usefixtures("seeded")
def test_changed_files_known_defaults_to_true(client: TestClient) -> None:
    assert post_select(client, changed_files=["src/shop/cart.py"]).json()["mode"] == "selective"


def test_unknown_repo_returns_404(client: TestClient) -> None:
    response = post_select(client, repo="nobody/nothing", changed_files=["a.py"])

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown repo 'nobody/nothing'"


@pytest.mark.usefixtures("seeded")
def test_select_requires_token(client: TestClient) -> None:
    assert post_select(client, headers={}, changed_files=["a.py"]).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert post_select(client, headers=wrong, changed_files=["a.py"]).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"repo": ""},
        {"repo": REPO, "changed_files": "src/a.py"},  # a string, not a list
        {"repo": REPO, "changed_files_known": "maybe"},
        {"repo": REPO, "unexpected": 1},
        {"repo": REPO, "changed_files": ["x" * 4097]},
    ],
    ids=["empty-repo", "files-not-list", "known-not-bool", "extra-field", "path-too-long"],
)
def test_invalid_body_returns_422(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post("/select", json=body, headers=AUTH)
    assert response.status_code == 422


def test_select_is_post_only(client: TestClient) -> None:
    response = client.get("/select", params={"repo": REPO}, headers=AUTH)
    assert response.status_code == 405
