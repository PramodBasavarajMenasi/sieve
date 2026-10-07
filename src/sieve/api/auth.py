"""Bearer-token auth, checked from headers before any request body is read."""

import secrets
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_PUBLIC_PATHS = frozenset({"/healthz", "/docs", "/redoc", "/openapi.json"})


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every path not in ``public_paths``.

    Runs as raw ASGI middleware so a rejected request never has its body read: a client
    without a valid token can't make the server spool a large upload. New routes are
    protected by default. With no token configured, protected paths fail closed (503).
    """

    def __init__(
        self,
        app: ASGIApp,
        token: str | None,
        public_paths: Iterable[str] = DEFAULT_PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self.token = token.encode() if token else None
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.public_paths:
            await self.app(scope, receive, send)
            return

        if self.token is None:
            response = JSONResponse(
                {"detail": "server has no SIEVE_API_TOKEN configured"}, status_code=503
            )
        elif _is_valid_bearer(Headers(scope=scope).get("authorization", ""), self.token):
            await self.app(scope, receive, send)
            return
        else:
            response = JSONResponse(
                {"detail": "invalid or missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        await response(scope, receive, send)


def _is_valid_bearer(header: str, token: bytes) -> bool:
    scheme, _, credentials = header.partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(
        credentials.strip().encode(), token
    )
