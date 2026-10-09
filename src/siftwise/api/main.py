"""FastAPI application entry point."""

from fastapi import FastAPI

from siftwise import __version__
from siftwise.api.auth import BearerAuthMiddleware
from siftwise.api.limits import BodySizeLimitMiddleware
from siftwise.api.routes import history, repos, runs, select
from siftwise.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="siftwise", version=__version__)
    app.state.settings = settings  # read by routes through siftwise.api.deps.get_app_settings
    # Starlette runs the last-added middleware first: auth rejects before any body is read.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes)
    app.add_middleware(BearerAuthMiddleware, token=settings.api_token)
    app.include_router(runs.router)
    app.include_router(history.router)
    app.include_router(select.router)
    app.include_router(repos.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
