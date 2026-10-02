"""PostgreSQL fixtures isolated from application tables."""

import os
import uuid

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


@pytest.fixture
def postgres_engine():
    settings = dotenv_values(".env")
    configured = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or settings.get("DATABASE_URL")
    if not configured:
        pytest.fail("Set TEST_DATABASE_URL to a PostgreSQL database for database tests.")
    url = make_url(configured)
    if url.get_backend_name() != "postgresql":
        pytest.fail("Database tests require PostgreSQL.")
    # Docker service names are only reachable inside the Docker network.
    if url.host == "db" and not os.path.exists("/.dockerenv"):
        url = url.set(host="localhost", port=int(os.getenv("POSTGRES_PORT") or settings.get("POSTGRES_PORT") or 5432))
    schema = "codex_test_" + uuid.uuid4().hex
    admin = create_engine(url, connect_args={"connect_timeout": 10})
    engine = None
    created = False
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        created = True
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 10})
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()
