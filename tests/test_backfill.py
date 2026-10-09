import email
import io
import json
import zipfile
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from scripts import backfill as bf
from scripts.backfill import BackfillError, GitHubClient, SiftwiseClient, Summary

GH = "https://api.github.com"
SIFTWISE = "http://siftwise.test"
REPO = "acme/shop"
HEAD = "a" * 40
PARENT = "b" * 40
BASE = "c" * 40  # a pull request's base
BEFORE = "d" * 40  # a push's previous branch tip
FIXTURES = Path(__file__).parent / "fixtures"
PYTEST_XML = (FIXTURES / "pytest.xml").read_bytes()
GO_XML = (FIXTURES / "go.xml").read_bytes()


class FakeClock:
    """Records sleeps and advances time instead of sleeping."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- GitHub/siftwise fakes -------------------------------------------------------------------


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def gh_run(run_id: int = 101, **overrides: Any) -> dict[str, Any]:
    return {
        "id": run_id,
        "head_sha": HEAD,
        "head_branch": "main",
        "event": "push",
        "run_attempt": 1,
        "run_started_at": "2026-09-01T10:00:00Z",
        "created_at": "2026-09-01T09:59:00Z",
        **overrides,
    }


def artifact(artifact_id: int, name: str, *, expired: bool = False) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "name": name,
        "expired": expired,
        "archive_download_url": f"{GH}/repos/{REPO}/actions/artifacts/{artifact_id}/zip",
    }


def mock_runs(router: respx.MockRouter, *runs: dict[str, Any]) -> respx.Route:
    return router.get(f"{GH}/repos/{REPO}/actions/runs").respond(
        json={"total_count": len(runs), "workflow_runs": list(runs)}
    )


def mock_artifacts(router: respx.MockRouter, run_id: int, *artifacts: dict[str, Any]) -> None:
    router.get(f"{GH}/repos/{REPO}/actions/runs/{run_id}/artifacts").respond(
        json={"total_count": len(artifacts), "artifacts": list(artifacts)}
    )


def mock_download(router: respx.MockRouter, artifact_id: int, archive: bytes) -> respx.Route:
    """GitHub answers with a redirect to blob storage, like the real API."""
    blob = f"https://blob.example.net/artifact-{artifact_id}.zip"
    router.get(f"{GH}/repos/{REPO}/actions/artifacts/{artifact_id}/zip").respond(
        302, headers={"Location": blob}
    )
    return router.get(blob).respond(content=archive)


def mock_changed_files(router: respx.MockRouter, files: list[dict[str, str]]) -> respx.Route:
    """First-parent diff: what a push run without a check suite falls back to."""
    router.get(f"{GH}/repos/{REPO}/commits/{HEAD}").respond(json={"parents": [{"sha": PARENT}]})
    return mock_compare(router, PARENT, files)


def mock_compare(router: respx.MockRouter, base: str, files: list[dict[str, str]]) -> respx.Route:
    return router.get(f"{GH}/repos/{REPO}/compare/{base}...{HEAD}").respond(json={"files": files})


def pr(base: str, head: str = HEAD, number: int = 7) -> dict[str, Any]:
    return {"number": number, "head": {"sha": head}, "base": {"sha": base}}


def mock_siftwise(router: respx.MockRouter, *statuses: int) -> respx.Route:
    responses = [
        httpx.Response(status, json={"run_id": 1, "counts": {"total": 12}}) for status in statuses
    ]
    return router.post(f"{SIFTWISE}/runs").mock(side_effect=responses)


def standard_run(router: respx.MockRouter, run: dict[str, Any]) -> None:
    """One run with a single matching artifact containing one XML file."""
    mock_artifacts(router, run["id"], artifact(run["id"] * 10, "junit-results"))
    mock_download(router, run["id"] * 10, make_zip({"pytest.xml": PYTEST_XML}))


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}").respond(json={"default_branch": "main"})
        # By default siftwise has none of the runs; tests override router["lookup"].
        mock.get(f"{SIFTWISE}/runs/lookup", name="lookup").respond(404)
        yield mock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def run_backfill(clock: FakeClock, **kwargs: Any) -> tuple[Summary, list[str]]:
    lines: list[str] = []
    with (
        GitHubClient("gh-token", sleep=clock.sleep, clock=clock.time, **kwargs.pop("gh", {})) as gh,
        SiftwiseClient(
            SIFTWISE, "siftwise-token", defer_rollup=kwargs.pop("batch", False)
        ) as siftwise,
    ):
        summary = bf.backfill(gh, siftwise, REPO, echo=lines.append, **kwargs)
    return summary, lines


def counts(summary: Summary) -> dict[str, int]:
    return {outcome.value: n for outcome, n in summary.counts.items() if n}


def parse_upload(request: httpx.Request) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """Decode the multipart body sent to POST /runs into (metadata, files)."""
    raw = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
    message = email.message_from_bytes(raw + request.content)
    metadata: dict[str, Any] = {}
    files: list[tuple[str, bytes]] = []
    parts: list[Message] = message.get_payload()  # type: ignore[assignment]
    for part in parts:
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        if part.get_param("name", header="content-disposition") == "metadata":
            metadata = json.loads(payload)
        else:
            files.append((str(part.get_filename()), payload))
    return metadata, files


# --- normal run ---------------------------------------------------------------------------


def test_normal_run_is_uploaded(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(101, run_attempt=2))
    mock_artifacts(router, 101, artifact(1, "junit-results"), artifact(2, "coverage"))
    blob = mock_download(
        router,
        1,
        make_zip({"pytest.xml": PYTEST_XML, "nested/go.xml": GO_XML, "README.txt": b"not xml"}),
    )
    coverage = mock_download(router, 2, b"unused")
    mock_changed_files(
        router,
        [{"filename": "src/a.py"}, {"filename": "src/new.py", "previous_filename": "src/old.py"}],
    )
    siftwise = mock_siftwise(router, 201)

    summary, lines = run_backfill(clock)

    assert counts(summary) == {"new": 1}
    # The variant is the artifact name minus the pattern's literal part ("junit").
    assert "run 101 #2 aaaaaaa main [results]: new (12 results from 2 file(s))" in lines
    assert not coverage.called  # non-matching artifacts are never downloaded

    request = siftwise.calls.last.request
    assert request.headers["authorization"] == "Bearer siftwise-token"
    metadata, files = parse_upload(request)
    assert metadata == {
        "repo": REPO,
        "commit_sha": HEAD,
        "branch": "main",
        "is_main": True,
        "ci_run_id": "101",
        "run_attempt": 2,
        "started_at": "2026-09-01T10:00:00Z",
        "changed_files": ["src/a.py", "src/new.py", "src/old.py"],
        "changed_files_known": True,
        "variant": "results",
    }
    lookup = router["lookup"].calls.last.request
    assert dict(lookup.url.params) == {
        "repo": REPO,
        "ci_run_id": "101",
        "run_attempt": "2",
        "variant": "results",
    }
    assert lookup.headers["authorization"] == "Bearer siftwise-token"
    assert files == [
        ("junit-results/pytest.xml", PYTEST_XML),
        ("junit-results/nested/go.xml", GO_XML),
    ]

    # The GitHub token must not leak to the blob-storage redirect target.
    assert "authorization" not in blob.calls.last.request.headers
    assert clock.sleeps == []


def test_branch_and_pull_request_runs_are_not_main(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(
        router,
        gh_run(1, head_branch="feature"),
        gh_run(2, head_branch="main", event="pull_request", pull_requests=[pr(BASE)]),  # fork main
    )
    for run_id in (1, 2):
        standard_run(router, gh_run(run_id))
    mock_changed_files(router, [])
    mock_compare(router, BASE, [])
    siftwise = mock_siftwise(router, 201, 201)

    run_backfill(clock)

    assert [parse_upload(call.request)[0]["is_main"] for call in siftwise.calls] == [False, False]


def test_started_at_falls_back_to_created_at(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1, run_started_at=None))
    standard_run(router, gh_run(1))
    mock_changed_files(router, [])
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    assert parse_upload(siftwise.calls.last.request)[0]["started_at"] == "2026-09-01T09:59:00Z"


# --- re-runs ------------------------------------------------------------------------------


def lookup_finds(*run_ids: int, commit_sha: str = HEAD, variants: set[str] | None = None) -> Any:
    """A /runs/lookup side effect: siftwise has these runs (only these variants, if given)."""

    def respond(request: httpx.Request) -> httpx.Response:
        run_id = int(request.url.params["ci_run_id"])
        variant = request.url.params.get("variant")
        if run_id not in run_ids or (variants is not None and variant not in variants):
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"run_id": run_id, "commit_sha": commit_sha})

    return respond


def test_already_ingested_run_is_skipped_without_downloading(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1), gh_run(2))
    router["lookup"].mock(side_effect=lookup_finds(1))
    mock_artifacts(router, 1, artifact(10, "junit-results"))
    download_1 = mock_download(router, 10, make_zip({"pytest.xml": PYTEST_XML}))
    standard_run(router, gh_run(2))
    commit = mock_changed_files(router, [])
    siftwise = mock_siftwise(router, 201)

    summary, lines = run_backfill(clock)

    assert counts(summary) == {"new": 1, "skipped": 1}
    assert str(summary) == "1 new, 1 skipped, 0 no artifacts, 0 expired, 0 errors"
    assert lines[0].startswith("run 1 ") and lines[0].endswith("skipped (already ingested)")
    assert not download_1.called  # listed (to learn the variants), never downloaded
    assert siftwise.call_count == 1 and commit.call_count == 1  # only run 2


def test_rerun_of_full_backfill_downloads_nothing(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1), gh_run(2), gh_run(3))
    router["lookup"].mock(side_effect=lookup_finds(1, 2, 3))
    for run_id in (1, 2, 3):
        standard_run(router, gh_run(run_id))

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"skipped": 3}
    # Only the repo info, the runs list, artifact lists and the lookups were requested.
    assert {call.request.url.path for call in router.calls} == {
        f"/repos/{REPO}",
        f"/repos/{REPO}/actions/runs",
        *(f"/repos/{REPO}/actions/runs/{run_id}/artifacts" for run_id in (1, 2, 3)),
        "/runs/lookup",
    }


def test_run_ingested_concurrently_after_lookup_is_skipped(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1))
    standard_run(router, gh_run(1))
    mock_changed_files(router, [])
    mock_siftwise(router, 200)  # lookup said 404, but another uploader won the race

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"skipped": 1}


def test_lookup_with_different_commit_is_an_error(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1))
    standard_run(router, gh_run(1))
    router["lookup"].mock(side_effect=lookup_finds(1, commit_sha="c" * 40))

    summary, lines = run_backfill(clock)

    assert counts(summary) == {"errors": 1}
    assert "c" * 40 in lines[0] and HEAD in lines[0]


def test_lookup_failure_is_an_error_for_that_run(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1), gh_run(2))
    standard_run(router, gh_run(1))
    router["lookup"].mock(side_effect=[httpx.Response(500)])
    mock_artifacts(router, 2)  # no artifacts: no lookup for run 2

    summary, lines = run_backfill(clock)

    assert counts(summary) == {"errors": 1, "no artifacts": 1}
    assert "HTTPStatusError" in lines[0]


# --- artifacts ----------------------------------------------------------------------------


def test_missing_artifact(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1), gh_run(2))
    mock_artifacts(router, 1, artifact(10, "coverage-report"))
    mock_artifacts(router, 2)  # no artifacts at all
    compare = mock_changed_files(router, [])
    siftwise = mock_siftwise(router)

    summary, lines = run_backfill(clock)

    assert counts(summary) == {"no artifacts": 2}
    assert "no artifact matches '*junit*'" in lines[0]
    assert not siftwise.called
    assert not compare.called  # no GitHub calls wasted on runs with nothing to upload


def test_artifact_without_xml_counts_as_no_artifacts(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1))
    mock_artifacts(router, 1, artifact(10, "JUnit-Reports"))  # pattern is case-insensitive
    mock_download(router, 10, make_zip({"report.html": b"<html/>"}))
    siftwise = mock_siftwise(router)

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"no artifacts": 1}
    assert not siftwise.called


def test_custom_artifact_pattern(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1))
    mock_artifacts(router, 1, artifact(10, "test-results-linux"))
    mock_download(router, 10, make_zip({"r.xml": GO_XML}))
    mock_changed_files(router, [])
    mock_siftwise(router, 201)

    summary, _ = run_backfill(clock, pattern="test-results-*")

    assert counts(summary) == {"new": 1}


def test_expired_artifact_is_not_downloaded(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1))
    mock_artifacts(router, 1, artifact(10, "junit", expired=True))
    download = mock_download(router, 10, b"")
    siftwise = mock_siftwise(router)

    summary, lines = run_backfill(clock)

    assert counts(summary) == {"expired": 1}
    assert "expired: junit" in lines[0]
    assert not download.called
    assert not siftwise.called


def test_artifact_expiring_at_download_time(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1))
    mock_artifacts(router, 1, artifact(10, "junit"))
    router.get(f"{GH}/repos/{REPO}/actions/artifacts/10/zip").respond(410)

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"expired": 1}


def test_partially_expired_run_uploads_what_remains(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1))
    mock_artifacts(router, 1, artifact(10, "junit-a", expired=True), artifact(11, "junit-b"))
    mock_download(router, 11, make_zip({"go.xml": GO_XML}))
    mock_changed_files(router, [])
    siftwise = mock_siftwise(router, 201)

    summary, lines = run_backfill(clock)

    # Each artifact is its own variant: the expired one is reported, the other uploaded.
    assert counts(summary) == {"new": 1, "expired": 1}
    assert any("[a]: expired" in line for line in lines)
    metadata, files = parse_upload(siftwise.calls.last.request)
    assert (metadata["variant"], [name for name, _ in files]) == ("b", ["junit-b/go.xml"])


# --- matrix variants ----------------------------------------------------------------------

MATRIX = "*-pytest-junit-xml"


def test_matrix_artifacts_are_uploaded_as_separate_variants(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1))
    mock_artifacts(
        router,
        1,
        artifact(10, "3.12-ubuntu-latest-pytest-junit-xml"),
        artifact(11, "3.12-macos-latest-pytest-junit-xml"),
    )
    mock_download(router, 10, make_zip({"linux.xml": PYTEST_XML}))
    mock_download(router, 11, make_zip({"mac.xml": GO_XML}))
    compare = mock_changed_files(router, [{"filename": "src/a.py"}])
    siftwise = mock_siftwise(router, 201, 201)

    summary, lines = run_backfill(clock, pattern=MATRIX)

    assert counts(summary) == {"new": 2}
    uploads = [parse_upload(call.request) for call in siftwise.calls]
    assert [(meta["variant"], [name for name, _ in files]) for meta, files in uploads] == [
        ("3.12-ubuntu-latest", ["3.12-ubuntu-latest-pytest-junit-xml/linux.xml"]),
        ("3.12-macos-latest", ["3.12-macos-latest-pytest-junit-xml/mac.xml"]),
    ]
    assert {meta["ci_run_id"] for meta, _ in uploads} == {"1"}  # same CI run
    assert compare.call_count == 1  # the diff is fetched once per CI run
    assert lines[0].endswith("[3.12-ubuntu-latest]: new (12 results from 1 file(s))")


def test_rerun_uploads_only_the_missing_variant(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1))
    mock_artifacts(
        router,
        1,
        artifact(10, "3.12-ubuntu-latest-pytest-junit-xml"),
        artifact(11, "3.12-macos-latest-pytest-junit-xml"),
    )
    linux = mock_download(router, 10, make_zip({"linux.xml": PYTEST_XML}))
    mock_download(router, 11, make_zip({"mac.xml": GO_XML}))
    mock_changed_files(router, [])
    router["lookup"].mock(side_effect=lookup_finds(1, variants={"3.12-ubuntu-latest"}))
    siftwise = mock_siftwise(router, 201)

    summary, _ = run_backfill(clock, pattern=MATRIX)

    assert counts(summary) == {"skipped": 1, "new": 1}
    assert not linux.called
    assert parse_upload(siftwise.calls.last.request)[0]["variant"] == "3.12-macos-latest"


@pytest.mark.parametrize(
    ("names", "pattern", "expected"),
    [
        (["cli-tests-junit", "unit-tests-junit"], "*junit*",
         {"cli-tests-junit": "cli-tests", "unit-tests-junit": "unit-tests"}),
        (["3.10-ubuntu-latest-extensive-pytest-junit-xml"], "*junit*",
         {"3.10-ubuntu-latest-extensive-pytest-junit-xml":
              "3.10-ubuntu-latest-extensive-pytest-xml"}),
        (["Test-Results-Linux"], "test-results-*", {"Test-Results-Linux": "Linux"}),
        (["junit"], "*junit*", {"junit": "junit"}),  # nothing left: keep the name
        # Two artifacts would both become "a": use full names so neither is lost.
        (["junit-a", "a-junit"], "*junit*", {"junit-a": "junit-a", "a-junit": "a-junit"}),
    ],
)  # fmt: skip
def test_artifact_variants(names: list[str], pattern: str, expected: dict[str, str]) -> None:
    assert bf.artifact_variants(names, pattern) == expected


def test_oversized_artifact_is_rejected_before_decompressing() -> None:
    archive = make_zip({"big.xml": b" " * 2000})
    with pytest.raises(BackfillError, match="2000 bytes of XML"):
        bf.extract_xml(archive, "junit", budget=1000)


# --- changed files ------------------------------------------------------------------------


def uploaded_diff(siftwise: respx.Route) -> tuple[list[str], bool]:
    metadata = parse_upload(siftwise.calls.last.request)[0]
    return metadata["changed_files"], metadata["changed_files_known"]


def test_push_run_diffs_the_whole_push(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1, check_suite_id=55))
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/check-suites/55").respond(json={"before": BEFORE, "after": HEAD})
    mock_compare(router, BEFORE, [{"filename": "a.go"}, {"filename": "b.go"}])
    first_parent = router.get(f"{GH}/repos/{REPO}/commits/{HEAD}").respond(json={})
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    assert uploaded_diff(siftwise) == (["a.go", "b.go"], True)
    assert not first_parent.called


@pytest.mark.parametrize(
    "suite_response",
    [
        httpx.Response(200, json={"before": "0" * 40}),  # the push created the branch
        httpx.Response(200, json={"before": None}),
        httpx.Response(404, json={"message": "Not Found"}),
    ],
    ids=["new-branch", "no-before", "suite-lookup-fails"],
)
def test_push_run_falls_back_to_first_parent(
    router: respx.MockRouter, clock: FakeClock, suite_response: httpx.Response
) -> None:
    mock_runs(router, gh_run(1, check_suite_id=55))
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/check-suites/55").mock(return_value=suite_response)
    mock_changed_files(router, [{"filename": "head_commit_only.go"}])
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    assert uploaded_diff(siftwise) == (["head_commit_only.go"], True)


def test_pull_request_run_diffs_the_whole_pr(router: respx.MockRouter, clock: FakeClock) -> None:
    # Several PRs can share a branch commit; the one whose head is this run's sha wins.
    other = pr("e" * 40, head="f" * 40, number=8)
    mock_runs(router, gh_run(1, event="pull_request", pull_requests=[other, pr(BASE)]))
    standard_run(router, gh_run(1))
    mock_compare(router, BASE, [{"filename": "first_commit.go"}, {"filename": "last_commit.go"}])
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    assert uploaded_diff(siftwise) == (["first_commit.go", "last_commit.go"], True)


def test_fork_pull_request_base_is_looked_up(router: respx.MockRouter, clock: FakeClock) -> None:
    # GitHub leaves pull_requests empty for PRs from forks.
    mock_runs(router, gh_run(1, event="pull_request", pull_requests=[]))
    standard_run(router, gh_run(1))
    pulls = router.get(f"{GH}/repos/{REPO}/commits/{HEAD}/pulls").respond(json=[pr(BASE)])
    mock_compare(router, BASE, [{"filename": "fork_change.go"}])
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    assert pulls.called
    assert uploaded_diff(siftwise) == (["fork_change.go"], True)


@pytest.mark.parametrize(
    "pulls_response",
    [httpx.Response(200, json=[]), httpx.Response(404, json={"message": "Not Found"})],
    ids=["no-pr", "lookup-fails"],
)
def test_pull_request_without_a_base_is_unknown(
    router: respx.MockRouter, clock: FakeClock, pulls_response: httpx.Response
) -> None:
    mock_runs(router, gh_run(1, event="pull_request", pull_requests=[]))
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/commits/{HEAD}/pulls").mock(return_value=pulls_response)
    siftwise = mock_siftwise(router, 201)

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"new": 1}  # still uploaded, just without a diff
    assert uploaded_diff(siftwise) == ([], False)


def fork_pr_run(run_id: int = 1) -> dict[str, Any]:
    """A fork PR run whose commit GitHub no longer links to its (closed) PR."""
    return gh_run(
        run_id,
        event="pull_request",
        pull_requests=[],
        head_branch="master",
        head_repository={"owner": {"login": "forker"}},
        run_started_at="2026-09-01T10:00:00Z",
    )


def closed_pr(base: str, head: str, created: str, closed: str | None, number: int) -> Any:
    return {**pr(base, head=head, number=number), "created_at": created, "closed_at": closed}


def test_closed_pr_is_found_by_branch(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, fork_pr_run())
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/commits/{HEAD}/pulls").respond(json=[])
    branch_prs = router.get(f"{GH}/repos/{REPO}/pulls").respond(
        json=[closed_pr(BASE, HEAD, "2026-08-30T00:00:00Z", "2026-09-02T00:00:00Z", 7)]
    )
    mock_compare(router, BASE, [{"filename": "closed_pr.go"}])
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    params = branch_prs.calls.last.request.url.params
    assert (params["state"], params["head"]) == ("all", "forker:master")
    assert uploaded_diff(siftwise) == (["closed_pr.go"], True)


def test_branch_pr_is_matched_by_time_when_head_moved(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    # The fork's "master" was used by several PRs; the run's commit is no longer any PR's
    # head (later pushes moved it), so pick the PR that was open when the run started.
    mock_runs(router, fork_pr_run())
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/commits/{HEAD}/pulls").respond(json=[])
    router.get(f"{GH}/repos/{REPO}/pulls").respond(
        json=[
            closed_pr("e" * 40, "1" * 40, "2026-09-05T00:00:00Z", None, 9),  # later PR
            closed_pr(BASE, "2" * 40, "2026-08-30T00:00:00Z", "2026-09-03T00:00:00Z", 8),
            closed_pr("f" * 40, "3" * 40, "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", 5),
        ]
    )
    mock_compare(router, BASE, [{"filename": "pr8.go"}])
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    assert uploaded_diff(siftwise) == (["pr8.go"], True)


@pytest.mark.parametrize(
    "branch_response",
    [
        # Only PRs that weren't open when the run started: too ambiguous to use.
        httpx.Response(200, json=[closed_pr(BASE, "9" * 40, "2026-09-05T00:00:00Z", None, 9)]),
        httpx.Response(200, json=[]),
        httpx.Response(500, json={"message": "boom"}),
    ],
    ids=["no-pr-open-at-run-time", "no-prs", "lookup-fails"],
)
def test_branch_lookup_without_a_clear_match_is_unknown(
    router: respx.MockRouter, clock: FakeClock, branch_response: httpx.Response
) -> None:
    mock_runs(router, fork_pr_run())
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/commits/{HEAD}/pulls").respond(json=[])
    router.get(f"{GH}/repos/{REPO}/pulls").mock(return_value=branch_response)
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock, gh={"max_retries": 0})

    assert uploaded_diff(siftwise) == ([], False)


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch", "pull_request_target"])
def test_other_events_have_unknown_diffs(
    router: respx.MockRouter, clock: FakeClock, event: str
) -> None:
    mock_runs(router, gh_run(1, event=event))
    standard_run(router, gh_run(1))
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    assert uploaded_diff(siftwise) == ([], False)
    compared = [c for c in router.calls if "/compare/" in c.request.url.path]
    assert compared == []


@pytest.mark.parametrize(
    "compare_response",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(200, json={"files": [{"filename": f"f{i}.py"} for i in range(300)]}),
    ],
    ids=["compare-fails", "compare-truncated"],
)
def test_unknown_changed_files_are_flagged(
    router: respx.MockRouter, clock: FakeClock, compare_response: httpx.Response
) -> None:
    mock_runs(router, gh_run(1))
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/commits/{HEAD}").respond(json={"parents": [{"sha": PARENT}]})
    router.get(f"{GH}/repos/{REPO}/compare/{PARENT}...{HEAD}").mock(return_value=compare_response)
    siftwise = mock_siftwise(router, 201)

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"new": 1}  # the run is still uploaded
    metadata = parse_upload(siftwise.calls.last.request)[0]
    assert (metadata["changed_files"], metadata["changed_files_known"]) == ([], False)


def test_root_commit_changed_files_are_unknown(router: respx.MockRouter, clock: FakeClock) -> None:
    # Every file is new in a root commit; an empty "known" list would claim nothing changed.
    mock_runs(router, gh_run(1))
    standard_run(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/commits/{HEAD}").respond(json={"parents": []})
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    metadata = parse_upload(siftwise.calls.last.request)[0]
    assert (metadata["changed_files"], metadata["changed_files_known"]) == ([], False)


def test_empty_diff_is_known(router: respx.MockRouter, clock: FakeClock) -> None:
    # e.g. an empty merge commit: genuinely nothing changed, which is different from unknown.
    mock_runs(router, gh_run(1))
    standard_run(router, gh_run(1))
    mock_changed_files(router, [])
    siftwise = mock_siftwise(router, 201)

    run_backfill(clock)

    metadata = parse_upload(siftwise.calls.last.request)[0]
    assert (metadata["changed_files"], metadata["changed_files_known"]) == ([], True)


# --- rate limits and retries --------------------------------------------------------------


def test_rate_limit_waits_for_reset_then_retries(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    reset = int(clock.now) + 30
    router.get(f"{GH}/repos/{REPO}/actions/runs").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
                json={"message": "API rate limit exceeded"},
            ),
            httpx.Response(200, json={"workflow_runs": [gh_run(1)]}),
        ]
    )
    standard_run(router, gh_run(1))
    mock_changed_files(router, [])
    mock_siftwise(router, 201)

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"new": 1}
    assert clock.sleeps == [31.0]  # until reset, plus a 1s margin


@pytest.mark.parametrize(
    ("limited", "expected_sleep"),
    [
        (httpx.Response(429, headers={"Retry-After": "7"}), 7.0),
        (httpx.Response(403, headers={"Retry-After": "12"}), 12.0),  # secondary rate limit
        (httpx.Response(429), 60.0),  # no headers: GitHub asks for at least a minute
    ],
    ids=["429-retry-after", "403-retry-after", "429-bare"],
)
def test_secondary_rate_limits_are_retried(
    router: respx.MockRouter,
    clock: FakeClock,
    limited: httpx.Response,
    expected_sleep: float,
) -> None:
    mock_runs(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/actions/runs/1/artifacts").mock(
        side_effect=[limited, httpx.Response(200, json={"artifacts": []})]
    )

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"no artifacts": 1}
    assert clock.sleeps == [expected_sleep]


def test_exhausted_quota_on_success_delays_next_request(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    reset = int(clock.now) + 100
    router.get(f"{GH}/repos/{REPO}").respond(
        json={"default_branch": "main"},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
    )
    runs = mock_runs(router)

    run_backfill(clock)

    assert clock.sleeps == [101.0]
    assert runs.called  # proceeds after the wait


def test_server_errors_back_off_exponentially(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1))
    router.get(f"{GH}/repos/{REPO}/actions/runs/1/artifacts").mock(
        side_effect=[
            httpx.Response(502),
            httpx.ConnectError("connection reset"),
            httpx.Response(503),
            httpx.Response(200, json={"artifacts": []}),
        ]
    )

    summary, _ = run_backfill(clock)

    assert counts(summary) == {"no artifacts": 1}
    assert clock.sleeps == [2.0, 4.0, 8.0]


def test_exhausted_retries_count_as_error_and_continue(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1), gh_run(2))
    router.get(f"{GH}/repos/{REPO}/actions/runs/1/artifacts").respond(502)
    mock_artifacts(router, 2)

    summary, lines = run_backfill(clock, gh={"max_retries": 2})

    assert counts(summary) == {"errors": 1, "no artifacts": 1}
    assert "HTTPStatusError" in lines[0]
    assert clock.sleeps == [2.0, 4.0]


def test_plain_403_is_not_retried(router: respx.MockRouter, clock: FakeClock) -> None:
    router.get(f"{GH}/repos/{REPO}").respond(403, json={"message": "Resource not accessible"})

    with pytest.raises(httpx.HTTPStatusError):
        run_backfill(clock)
    assert clock.sleeps == []


# --- per-run errors, pagination -----------------------------------------------------------


def test_per_run_errors_are_counted_and_backfill_continues(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1), gh_run(2), gh_run(3))
    standard_run(router, gh_run(1))
    mock_artifacts(router, 2, artifact(20, "junit"))
    mock_download(router, 20, b"this is not a zip")
    standard_run(router, gh_run(3))
    mock_changed_files(router, [])
    mock_siftwise(router, 409, 201)

    summary, lines = run_backfill(clock)

    assert counts(summary) == {"errors": 2, "new": 1}
    assert "siftwise returned 409" in lines[0]
    assert "BadZipFile" in lines[1]


def test_pagination_stops_at_max_runs(
    router: respx.MockRouter, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bf, "PER_PAGE", 2)
    pages = {"1": [gh_run(1), gh_run(2)], "2": [gh_run(3), gh_run(4)], "3": [gh_run(5)]}
    runs = router.get(f"{GH}/repos/{REPO}/actions/runs").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"workflow_runs": pages[request.url.params["page"]]}
        )
    )
    for run_id in range(1, 6):
        mock_artifacts(router, run_id)

    summary, lines = run_backfill(clock, max_runs=3)

    assert counts(summary) == {"no artifacts": 3}
    assert [line.split()[1] for line in lines] == ["1", "2", "3"]
    assert [call.request.url.params["page"] for call in runs.calls] == ["1", "2"]
    assert runs.calls[0].request.url.params["status"] == "completed"


def test_workflow_filter_uses_workflow_runs_endpoint(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    runs = router.get(f"{GH}/repos/{REPO}/actions/workflows/ci.yml/runs").respond(
        json={"workflow_runs": []}
    )

    run_backfill(clock, workflow="ci.yml")

    assert runs.called


# --- CLI ----------------------------------------------------------------------------------


# --- batch mode ---------------------------------------------------------------------------


def test_batch_mode_defers_rollup_and_runs_it_once(
    router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_runs(router, gh_run(1), gh_run(2))
    standard_run(router, gh_run(1))
    standard_run(router, gh_run(2))
    mock_changed_files(router, [])
    siftwise = mock_siftwise(router, 201, 201)
    rollup = router.post(f"{SIFTWISE}/repos/{REPO}/rollup").respond(
        json={"repo": REPO, "tests_updated": 7, "window_days": 90, "seconds": 1.5}
    )

    summary, lines = run_backfill(clock, batch=True)

    assert counts(summary) == {"new": 2}
    assert [call.request.url.params.get("defer_rollup") for call in siftwise.calls] == [
        "true",
        "true",
    ]
    assert rollup.call_count == 1  # once, after every upload
    assert lines[-1] == "rollup: 7 tests updated in 1.5s (window 90 days)"
    assert summary.rollup_error is None


def test_normal_mode_does_not_defer_or_roll_up(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router, gh_run(1))
    standard_run(router, gh_run(1))
    mock_changed_files(router, [])
    siftwise = mock_siftwise(router, 201)
    rollup = router.post(f"{SIFTWISE}/repos/{REPO}/rollup").respond(json={})

    run_backfill(clock)

    assert "defer_rollup" not in siftwise.calls.last.request.url.params
    assert not rollup.called


def test_batch_rollup_failure_is_reported(router: respx.MockRouter, clock: FakeClock) -> None:
    mock_runs(router)
    router.post(f"{SIFTWISE}/repos/{REPO}/rollup").respond(500)

    summary, lines = run_backfill(clock, batch=True)

    assert summary.rollup_error is not None and "500" in summary.rollup_error
    assert lines[-1].startswith("rollup failed: HTTPStatusError")


def test_cli_batch_rollup_failure_exits_1(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "siftwise-token")
    mock_runs(router)
    router.post(f"{SIFTWISE}/repos/{REPO}/rollup").respond(503)

    result = CliRunner().invoke(bf.app, ["--repo", REPO, "--server", SIFTWISE, "--batch"])

    assert result.exit_code == 1
    assert f"POST {SIFTWISE}/repos/{REPO}/rollup" in result.stderr


def test_cli_prints_summary_and_exit_code(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "siftwise-token")
    mock_runs(router, gh_run(1), gh_run(2))
    standard_run(router, gh_run(1))
    standard_run(router, gh_run(2))
    mock_changed_files(router, [])
    mock_siftwise(router, 201, 500)

    result = CliRunner().invoke(bf.app, ["--repo", REPO, "--server", SIFTWISE])

    assert "summary: 1 new, 0 skipped, 0 no artifacts, 0 expired, 1 errors" in result.stdout
    assert result.exit_code == 1  # any per-run error fails the command


@pytest.mark.parametrize(
    ("env", "args", "message"),
    [
        ({}, ["--repo", REPO], "set GITHUB_TOKEN and SIFTWISE_API_TOKEN"),
        ({"GITHUB_TOKEN": "x"}, ["--repo", REPO], "set SIFTWISE_API_TOKEN"),
        ({"GITHUB_TOKEN": "x", "SIFTWISE_API_TOKEN": "y"}, ["--repo", "shop"], "owner/name"),
    ],
    ids=["no-tokens", "no-siftwise-token", "bad-repo"],
)
def test_cli_rejects_bad_configuration(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], args: list[str], message: str
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SIFTWISE_API_TOKEN", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    result = CliRunner().invoke(bf.app, args)

    assert result.exit_code == 2
    assert message in result.stderr


def test_cli_reports_fatal_github_errors(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "bad")
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "y")
    router.get(f"{GH}/repos/{REPO}").respond(401, json={"message": "Bad credentials"})

    result = CliRunner().invoke(bf.app, ["--repo", REPO])

    assert result.exit_code == 2
    assert "401" in result.stderr
