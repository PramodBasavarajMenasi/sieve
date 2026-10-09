import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from siftwise import __version__
from siftwise.api.main import app
from siftwise.cli.main import app as cli_app
from siftwise.config import Settings


def test_healthz() -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFTWISE_DATABASE_URL", "postgresql+psycopg://u:p@db/x")
    monkeypatch.setenv("SIFTWISE_API_TOKEN", "secret")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql+psycopg://u:p@db/x"
    assert settings.api_token == "secret"


def test_cli_version() -> None:
    result = CliRunner().invoke(cli_app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
