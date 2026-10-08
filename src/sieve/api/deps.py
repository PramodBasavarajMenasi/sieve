"""Shared FastAPI dependencies."""

from fastapi import Request

from sieve.config import Settings


def get_app_settings(request: Request) -> Settings:
    """The settings the app was created with (see ``create_app``)."""
    settings: Settings = request.app.state.settings
    return settings
