"""Request body size limit, enforced while the body streams in."""

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """Reject request bodies over ``max_bytes`` with 413.

    Checks ``Content-Length`` up front, and also counts streamed bytes so chunked uploads
    without a length header can't bypass the limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        detail = f"request body exceeds {self.max_bytes} bytes"
        too_large = JSONResponse({"detail": detail}, status_code=413)
        content_length = Headers(scope=scope).get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            await too_large(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # An HTTPException, so FastAPI's body parsing re-raises it unchanged (rather
                    # than wrapping it as a 400) and its exception handler renders the 413.
                    raise HTTPException(413, detail)
            return message

        await self.app(scope, limited_receive, send)
