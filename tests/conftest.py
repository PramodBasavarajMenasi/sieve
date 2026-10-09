import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("SIFTWISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SIFTWISE_TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def db_engine(db_url: str) -> Iterator[Engine]:
    """Engine for a freshly migrated test database (rebuilt once per session)."""
    config = alembic_config(db_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Session whose work, including commits, is rolled back after each test."""
    with db_engine.connect() as connection, connection.begin() as transaction:
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        yield session
        session.close()
        transaction.rollback()
