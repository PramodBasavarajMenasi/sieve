"""FastAPI application entry point."""

from fastapi import FastAPI

from sieve import __version__

app = FastAPI(title="sieve", version=__version__)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
