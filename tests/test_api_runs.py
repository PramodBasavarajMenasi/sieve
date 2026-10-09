import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from siftwise.api.main import create_app
from siftwise.config import Settings
from siftwise.core.junit import Status
from siftwise.core.models import ChangedFile, Repo, Run, TestResult
from siftwise.db import get_session

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MAX_UPLOAD = 256 * 1024
SHA = "a1b2c3d4e5" * 4
OTHER_SHA = "f0" * 20


def make_client(session: Session, **settings: Any) -> TestClient:
    config = Settings(
        _env_file=None, **{"api_token": TOKEN, "max_upload_bytes": MAX_UPLOAD, **settings}
    )
    app: FastAPI = create_app(config)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    with make_client(db_session) as test_client:
        yield test_client


def metadata(**overrides: Any) -> dict[str, Any]:
    return {
        "repo": "acme/shop",
        "commit_sha": SHA.upper(),  # stored lower-cased
        "branch": "main",
        "is_main": True,
        "ci_run_id": "9001",
        "run_attempt": 1,
        "changed_files": ["src/cart.py", "./src/cart.py", "src\\pay.py", ""],
        **overrides,
    }


def post_run(
    client: TestClient,
    files: list[tuple[str, bytes]],
    meta: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    response: httpx.Response = client.post(
        "/runs",
        files=[("files", (name, content, "application/xml")) for name, content in files],
        data={"metadata": json.dumps(meta if meta is not None else metadata())},
        headers=AUTH if headers is None else headers,
    )
    return response


def fixture(name: str) -> tuple[str, bytes]:
    return name, (FIXTURES / name).read_bytes()


def count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- happy path ---------------------------------------------------------------------------


def test_new_upload_creates_run(client: TestClient, db_session: Session) -> None:
    response = post_run(client, [fixture("pytest.xml")])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["repo"] == "acme/shop"
    assert body["counts"] == {"passed": 3, "failed": 1, "skipped": 2, "error": 1, "total": 7}

    run = db_session.get(Run, body["run_id"])
    assert run is not None
    assert (run.commit_sha, run.branch, run.is_main, run.ci_run_id, run.run_attempt) == (
        SHA,
        "main",
        True,
        "9001",
        1,
    )
    repo = db_session.scalars(select(Repo)).one()
    assert repo.name == "acme/shop" and run.repo_id == repo.id

    paths = db_session.scalars(select(ChangedFile.path).order_by(ChangedFile.path)).all()
    assert paths == ["src/cart.py", "src/pay.py"]  # normalized, de-duplicated, empty dropped

    failed = db_session.scalars(
        select(TestResult).where(
            TestResult.test_id == "tests.test_math.TestDivide::test_divide_by_zero"
        )
    ).one()
    assert (failed.status, failed.duration_ms, failed.message) == (
        Status.FAILED,
        123,
        "assert 1 == 2",
    )


def test_second_repo_upload_reuses_repo(client: TestClient, db_session: Session) -> None:
    assert post_run(client, [fixture("go.xml")]).status_code == 201
    assert post_run(client, [fixture("go.xml")], metadata(ci_run_id="9002")).status_code == 201
    assert count(db_session, Repo) == 1
    assert count(db_session, Run) == 2


def test_multiple_files(client: TestClient, db_session: Session) -> None:
    response = post_run(
        client,
        [
            fixture("pytest.xml"),
            fixture("jest.xml"),
            fixture("go.xml"),
            fixture("maven-surefire.xml"),
        ],
    )

    assert response.status_code == 201, response.text
    # pytest 7 + jest 5 + go 5 + maven 11 (6 testcases, 5 extra rerun/flaky attempts)
    assert response.json()["counts"] == {
        "passed": 3 + 3 + 2 + 3,
        "failed": 1 + 1 + 2 + 5,
        "skipped": 2 + 1 + 1 + 1,
        "error": 1 + 0 + 0 + 2,
        "total": 28,
    }
    assert count(db_session, TestResult) == 28


def test_same_test_in_several_files_gets_increasing_attempts(
    client: TestClient, db_session: Session
) -> None:
    response = post_run(client, [fixture("go.xml"), fixture("go.xml")])

    assert response.status_code == 201, response.text
    attempts = db_session.scalars(
        select(TestResult.attempt)
        .where(TestResult.test_id == "github.com/acme/shop/cart::TestAddItem")
        .order_by(TestResult.attempt)
    ).all()
    assert attempts == [1, 2]


def test_long_message_is_truncated_with_marker(client: TestClient, db_session: Session) -> None:
    xml = (
        '<testsuite><testcase classname="c" name="n"><failure message="'
        + "x" * 10_000
        + '"/></testcase></testsuite>'
    ).encode()

    assert post_run(client, [("long.xml", xml)]).status_code == 201
    message = db_session.scalars(select(TestResult.message)).one()
    assert message is not None
    assert message.endswith("…[truncated]")
    assert len(message.encode()) <= 4096


# --- idempotency --------------------------------------------------------------------------


def test_duplicate_upload_returns_existing_run(client: TestClient, db_session: Session) -> None:
    first = post_run(client, [fixture("pytest.xml")])
    second = post_run(client, [fixture("pytest.xml")])

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == {**first.json(), "created": False}
    assert count(db_session, Run) == 1
    assert count(db_session, TestResult) == 7


def test_duplicate_upload_skips_parsing(client: TestClient) -> None:
    assert post_run(client, [fixture("pytest.xml")]).status_code == 201
    # Re-uploads short-circuit before parsing, so even a broken file returns the stored run.
    assert post_run(client, [("broken.xml", b"<nope")]).status_code == 200


def test_new_run_attempt_is_a_new_run(client: TestClient) -> None:
    first = post_run(client, [fixture("pytest.xml")])
    rerun = post_run(client, [fixture("pytest.xml")], metadata(run_attempt=2))

    assert rerun.status_code == 201
    assert rerun.json()["run_id"] != first.json()["run_id"]


def test_uploads_without_ci_run_id_are_never_deduplicated(client: TestClient) -> None:
    meta = metadata(ci_run_id=None)
    assert post_run(client, [fixture("go.xml")], meta).status_code == 201
    assert post_run(client, [fixture("go.xml")], meta).status_code == 201


def test_concurrent_duplicate_returns_existing_run(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = post_run(client, [fixture("pytest.xml")])
    # Simulate losing the race: the pre-check misses, so the insert hits the unique constraint.
    monkeypatch.setattr("siftwise.api.routes.runs.find_existing_run", lambda session, meta: None)

    second = post_run(client, [fixture("pytest.xml")])

    assert second.status_code == 200, second.text
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["counts"]["total"] == 7
    assert count(db_session, Run) == 1
    assert count(db_session, TestResult) == 7


def test_reupload_with_different_commit_returns_409(
    client: TestClient, db_session: Session
) -> None:
    assert post_run(client, [fixture("pytest.xml")]).status_code == 201

    response = post_run(client, [fixture("pytest.xml")], metadata(commit_sha=OTHER_SHA))

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert SHA in detail and OTHER_SHA in detail
    assert count(db_session, Run) == 1
    assert count(db_session, TestResult) == 7


def test_reupload_commit_sha_comparison_ignores_case(client: TestClient) -> None:
    assert (
        post_run(client, [fixture("go.xml")], metadata(commit_sha=SHA.upper())).status_code == 201
    )
    assert post_run(client, [fixture("go.xml")], metadata(commit_sha=SHA)).status_code == 200


def test_concurrent_reupload_with_different_commit_returns_409(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert post_run(client, [fixture("pytest.xml")]).status_code == 201
    monkeypatch.setattr("siftwise.api.routes.runs.find_existing_run", lambda session, meta: None)

    response = post_run(client, [fixture("pytest.xml")], metadata(commit_sha=OTHER_SHA))

    assert response.status_code == 409
    assert count(db_session, Run) == 1


# --- changed_files_known ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [({}, True), ({"changed_files_known": True}, True), ({"changed_files_known": False}, False)],
    ids=["default", "true", "false"],
)
def test_changed_files_known_is_stored(
    client: TestClient, db_session: Session, overrides: dict[str, Any], expected: bool
) -> None:
    response = post_run(client, [fixture("go.xml")], metadata(**overrides))

    run = db_session.get(Run, response.json()["run_id"])
    assert run is not None
    assert run.changed_files_known is expected


# --- GET /runs/lookup ---------------------------------------------------------------------


def lookup(client: TestClient, **params: Any) -> httpx.Response:
    response: httpx.Response = client.get(
        "/runs/lookup",
        params={"repo": "acme/shop", "ci_run_id": "9001", **params},
        headers=AUTH,
    )
    return response


def test_lookup_finds_ingested_run(client: TestClient) -> None:
    run_id = post_run(client, [fixture("go.xml")], metadata(run_attempt=2)).json()["run_id"]

    response = lookup(client, run_attempt=2)

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "repo": "acme/shop",
        "ci_run_id": "9001",
        "run_attempt": 2,
        "variant": None,
        "commit_sha": SHA,
    }


@pytest.mark.parametrize(
    "params",
    [
        {"ci_run_id": "9999"},
        {"run_attempt": 3},
        {"repo": "acme/other"},
        {},  # run_attempt defaults to 1; only attempt 2 was ingested
    ],
    ids=["other-run", "other-attempt", "other-repo", "default-attempt"],
)
def test_lookup_returns_404_when_not_ingested(client: TestClient, params: dict[str, Any]) -> None:
    post_run(client, [fixture("go.xml")], metadata(run_attempt=2))

    response = lookup(client, **params)

    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [{"repo": ""}, {"ci_run_id": ""}, {"run_attempt": 0}],
    ids=["empty-repo", "empty-ci-run-id", "attempt-0"],
)
def test_lookup_validates_params(client: TestClient, params: dict[str, Any]) -> None:
    assert lookup(client, **params).status_code == 422


def test_lookup_requires_token(client: TestClient) -> None:
    response = client.get("/runs/lookup", params={"repo": "acme/shop", "ci_run_id": "1"})
    assert response.status_code == 401


# --- matrix variants ----------------------------------------------------------------------


def test_each_variant_is_its_own_run(client: TestClient, db_session: Session) -> None:
    linux = post_run(client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12"))
    macos = post_run(client, [fixture("go.xml")], metadata(variant="macos-py3.12"))
    plain = post_run(client, [fixture("go.xml")], metadata())

    assert (linux.status_code, macos.status_code, plain.status_code) == (201, 201, 201)
    assert len({linux.json()["run_id"], macos.json()["run_id"], plain.json()["run_id"]}) == 3
    assert count(db_session, Run) == 3


def test_reupload_of_one_variant_does_not_block_others(
    client: TestClient, db_session: Session
) -> None:
    first = post_run(client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12"))
    again = post_run(client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12"))
    other = post_run(client, [fixture("go.xml")], metadata(variant="macos-py3.12"))

    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 201)
    assert again.json()["run_id"] == first.json()["run_id"]
    assert count(db_session, Run) == 2


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_variant_means_no_variant(client: TestClient, blank: str) -> None:
    first = post_run(client, [fixture("go.xml")], metadata())
    again = post_run(client, [fixture("go.xml")], metadata(variant=blank))

    assert again.status_code == 200
    assert again.json()["run_id"] == first.json()["run_id"]


def test_concurrent_duplicate_of_a_variant_returns_existing_run(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = post_run(client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12"))
    monkeypatch.setattr("siftwise.api.routes.runs.find_existing_run", lambda session, meta: None)

    second = post_run(client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12"))

    assert second.status_code == 200
    assert second.json()["run_id"] == first.json()["run_id"]
    assert count(db_session, Run) == 1


def test_variant_conflict_names_the_variant(client: TestClient) -> None:
    post_run(client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12"))
    response = post_run(
        client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12", commit_sha=OTHER_SHA)
    )
    assert response.status_code == 409
    assert "variant 'ubuntu-py3.12'" in response.json()["detail"]


def test_lookup_by_variant(client: TestClient) -> None:
    linux = post_run(client, [fixture("go.xml")], metadata(variant="ubuntu-py3.12")).json()

    found = lookup(client, variant="ubuntu-py3.12")
    assert found.status_code == 200
    assert (found.json()["run_id"], found.json()["variant"]) == (linux["run_id"], "ubuntu-py3.12")

    missing = lookup(client, variant="macos-py3.12")
    assert missing.status_code == 404
    assert "variant 'macos-py3.12'" in missing.json()["detail"]
    # Without a variant, only a run uploaded without one matches.
    assert lookup(client).status_code == 404
    assert lookup(client, variant="").status_code == 404


def test_variant_too_long_returns_422(client: TestClient) -> None:
    response = post_run(client, [fixture("go.xml")], metadata(variant="x" * 256))
    assert response.status_code == 422


# --- auth ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
    ],
    ids=["missing", "wrong", "not-bearer"],
)
def test_bad_token_is_rejected(
    client: TestClient, db_session: Session, headers: dict[str, str]
) -> None:
    response = post_run(client, [fixture("pytest.xml")], headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert count(db_session, Run) == 0


def test_unconfigured_token_fails_closed(db_session: Session) -> None:
    with make_client(db_session, api_token=None) as client:
        response = post_run(client, [fixture("pytest.xml")])
    assert response.status_code == 503
    assert "SIFTWISE_API_TOKEN" in response.json()["detail"]


# --- bad input ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"<testsuite><testcase", "invalid JUnit XML"),
        (b"<html/>", "unexpected root element <html>"),
    ],
)
def test_bad_xml_returns_400(
    client: TestClient, db_session: Session, content: bytes, error: str
) -> None:
    response = post_run(client, [fixture("pytest.xml"), ("broken.xml", content)])

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("broken.xml: ")  # names the offending file
    assert error in detail
    assert count(db_session, Run) == 0  # nothing stored, not even the valid file


@pytest.mark.parametrize(
    "sha",
    [
        SHA[:7],  # abbreviated
        SHA[:39],
        SHA + "a",
        "a" * 64,  # SHA-256 object format is not accepted
        "g" * 40,  # right length, not hex
        "",
    ],
    ids=["short-7", "len-39", "len-41", "len-64", "non-hex", "empty"],
)
def test_commit_sha_must_be_full_40_char_hex(
    client: TestClient, db_session: Session, sha: str
) -> None:
    response = post_run(client, [fixture("pytest.xml")], metadata(commit_sha=sha))

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["metadata", "commit_sha"]
    assert count(db_session, Run) == 0


def test_commit_sha_surrounding_whitespace_is_trimmed(client: TestClient) -> None:
    response = post_run(client, [fixture("go.xml")], metadata(commit_sha=f" {SHA}\n"))
    assert response.status_code == 201


def test_unknown_metadata_field_returns_422(client: TestClient) -> None:
    response = post_run(client, [fixture("pytest.xml")], metadata(commit="abc1234"))
    assert response.status_code == 422


def test_missing_files_returns_422(client: TestClient) -> None:
    response = client.post("/runs", data={"metadata": json.dumps(metadata())}, headers=AUTH)
    assert response.status_code == 422


# --- size limit ---------------------------------------------------------------------------


def test_oversized_upload_returns_413(client: TestClient, db_session: Session) -> None:
    response = post_run(client, [("big.xml", b" " * (MAX_UPLOAD + 1))])

    assert response.status_code == 413
    assert response.json() == {"detail": f"request body exceeds {MAX_UPLOAD} bytes"}
    assert count(db_session, Run) == 0


def test_oversized_chunked_upload_without_content_length_returns_413(
    client: TestClient, db_session: Session
) -> None:
    def body() -> Iterator[bytes]:
        yield b'--b\r\nContent-Disposition: form-data; name="files"; filename="big.xml"\r\n\r\n'
        for _ in range(MAX_UPLOAD // 1024 + 1):
            yield b" " * 1024

    response = client.post(
        "/runs",
        content=body(),
        headers={**AUTH, "Content-Type": "multipart/form-data; boundary=b"},
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json() == {"detail": f"request body exceeds {MAX_UPLOAD} bytes"}
    assert count(db_session, Run) == 0
