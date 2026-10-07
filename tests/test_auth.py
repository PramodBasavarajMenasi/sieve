"""BearerAuthMiddleware: rejects before the request body is read. No database needed."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from sieve.api.auth import BearerAuthMiddleware
from sieve.api.main import create_app
from sieve.config import Settings

TOKEN = "s3cret"


async def inner_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Stand-in for the real app: reads the whole body, like a multipart upload would."""
    while (await receive()).get("more_body", False):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def call(middleware: BearerAuthMiddleware, path: str, headers: dict[str, str]) -> tuple[int, int]:
    """Drive the middleware with a 50-chunk body. Returns (status, body chunks read)."""
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    chunks_read = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal chunks_read
        chunks_read += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": chunks_read < 50}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent[0]["status"], chunks_read


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer"},
    ],
    ids=["missing", "wrong-token", "wrong-scheme", "empty"],
)
def test_unauthenticated_request_body_is_never_read(headers: dict[str, str]) -> None:
    status, chunks_read = call(BearerAuthMiddleware(inner_app, TOKEN), "/runs", headers)
    assert status == 401
    assert chunks_read == 0


def test_unconfigured_token_fails_closed_without_reading_body() -> None:
    status, chunks_read = call(
        BearerAuthMiddleware(inner_app, None), "/runs", {"Authorization": f"Bearer {TOKEN}"}
    )
    assert (status, chunks_read) == (503, 0)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
def test_valid_token_passes_through(scheme: str) -> None:
    status, chunks_read = call(
        BearerAuthMiddleware(inner_app, TOKEN), "/runs", {"Authorization": f"{scheme} {TOKEN}"}
    )
    assert (status, chunks_read) == (200, 50)


def test_public_paths_skip_auth() -> None:
    status, _ = call(BearerAuthMiddleware(inner_app, TOKEN), "/healthz", {})
    assert status == 200


# --- wired into the real app --------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(_env_file=None, api_token=TOKEN, max_upload_bytes=1024)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_auth_runs_before_size_limit(client: TestClient) -> None:
    oversized = {"files": ("big.xml", b" " * 4096, "application/xml")}

    assert client.post("/runs", files=oversized).status_code == 401
    authed = client.post("/runs", files=oversized, headers={"Authorization": f"Bearer {TOKEN}"})
    assert authed.status_code == 413


def test_unknown_paths_are_protected_by_default(client: TestClient) -> None:
    # 401 rather than 404: new routes can't be accidentally left open.
    assert client.get("/no-such-route").status_code == 401


def test_healthz_and_docs_are_public(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/openapi.json").status_code == 200
