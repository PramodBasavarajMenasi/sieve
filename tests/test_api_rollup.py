"""Batch ingest: POST /runs?defer_rollup=true, then POST /repos/{repo}/rollup."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sieve.api.main import create_app
from sieve.config import Settings
from sieve.core.models import TestStats
from sieve.db import get_session

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO = "acme/shop"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


def xml(**tests: str) -> bytes:
    cases = "".join(
        f'<testcase classname="t" name="{name}">'
        + ("<failure/>" if status == "failed" else "")
        + "</testcase>"
        for name, status in tests.items()
    )
    return f"<testsuite>{cases}</testsuite>".encode()


def upload(client: TestClient, n: int, defer: bool, **tests: str) -> httpx.Response:
    meta = {
        "repo": REPO,
        "commit_sha": f"{n:040x}",
        "branch": "main",
        "is_main": True,
        "ci_run_id": str(n),
        "started_at": f"2026-09-0{n}T00:00:00Z",
    }
    response: httpx.Response = client.post(
        "/runs",
        params={"defer_rollup": "true"} if defer else None,
        files={"files": ("r.xml", xml(**tests))},
        data={"metadata": json.dumps(meta)},
        headers=AUTH,
    )
    return response


def stats_rows(session: Session) -> dict[str, Any]:
    return {
        s.test_id: (s.runs, s.failures, s.broken_on_main_since_sha)
        for s in session.scalars(select(TestStats))
    }


def test_deferred_upload_leaves_stats_stale(client: TestClient, db_session: Session) -> None:
    response = upload(client, 1, defer=True, a="passed", b="failed")

    assert response.status_code == 201
    assert response.json()["stats_deferred"] is True
    assert db_session.scalar(select(func.count()).select_from(TestStats)) == 0


def test_normal_upload_reports_stats_not_deferred(client: TestClient) -> None:
    assert upload(client, 1, defer=False, a="passed").json()["stats_deferred"] is False


def test_rollup_recomputes_the_whole_repo(client: TestClient, db_session: Session) -> None:
    upload(client, 1, defer=True, a="passed", b="passed")
    upload(client, 2, defer=True, a="failed", b="passed", c="passed")

    response = client.post(f"/repos/{REPO}/rollup", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["repo"], body["tests_updated"], body["window_days"]) == (REPO, 3, 90)
    assert body["seconds"] >= 0
    assert stats_rows(db_session) == {
        "t::a": (2, 1, f"{2:040x}"),  # broken on main since run 2
        "t::b": (2, 0, None),
        "t::c": (1, 0, None),
    }


def test_rollup_matches_per_upload_ingest(client: TestClient, db_session: Session) -> None:
    upload(client, 1, defer=False, a="failed")
    upload(client, 2, defer=False, a="passed")
    upload(client, 3, defer=False, a="failed")
    per_upload = stats_rows(db_session)

    client.post(f"/repos/{REPO}/rollup", headers=AUTH)

    assert stats_rows(db_session) == per_upload  # same definitions, either way
    client.post(f"/repos/{REPO}/rollup", headers=AUTH)
    assert stats_rows(db_session) == per_upload  # idempotent


def test_rollup_unknown_repo_returns_404(client: TestClient) -> None:
    response = client.post("/repos/nobody/nothing/rollup", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown repo 'nobody/nothing'"


def test_rollup_requires_token(client: TestClient) -> None:
    assert client.post(f"/repos/{REPO}/rollup").status_code == 401
