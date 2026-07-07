"""SQLAlchemy engine / session / Base for all services.

Lives in `shared/` (mounted at /shared) so etl, notebook, and the api all import the
SAME models — no per-service duplication. `get_db()` is the FastAPI dependency used
by the api in the web-tool phase.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# DATABASE_URL is normally injected by docker-compose; also try common .env locations
# so scripts run outside compose still work.
load_dotenv("/shared/.env")
load_dotenv("../.env")
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL environment variable is not set. "
        "Check your .env / docker-compose configuration."
    )

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # verify connections before use
    pool_recycle=3600,    # recycle after 1h
    pool_size=10,
    max_overflow=20,
)

# We need to create Python classes that represent tables, so we need Base
Base = declarative_base() # inheriting Base means "this class IS a table"
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Provide a database session for one request, then close it (FastAPI dependency).

    Yields:
        A SQLAlchemy Session that is closed automatically when the request ends.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
