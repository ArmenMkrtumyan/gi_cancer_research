"""Shared fixtures. These are integration tests: they run against a real Postgres.

They are read-only with respect to scientific data — nothing here inserts, updates or
deletes an annotation. The migration test re-runs the migration runner, which is a no-op
once applied and is the property being asserted.

Run from the api container (it has the app, the models and the API on its path):
    docker exec gi_app-api-1 python -m pytest /tests -q
"""

import os
import sys

import pytest

for path in ("/shared", "/api", "/etl"):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(scope="session")
def engine():
    """The application's SQLAlchemy engine."""
    from Database.database import engine as e
    return e


@pytest.fixture()
def session():
    """A database session, rolled back and closed after each test."""
    from Database.database import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(scope="session")
def client():
    """FastAPI test client, or skip when the HTTP test dependency is unavailable."""
    pytest.importorskip("httpx", reason="httpx is required by fastapi.testclient")
    from fastapi.testclient import TestClient

    import main
    return TestClient(main.app)
