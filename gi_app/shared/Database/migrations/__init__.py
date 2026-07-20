"""Minimal versioned migration runner.

The project bootstraps with `Base.metadata.create_all()`, which creates *missing*
tables but never alters an existing one. That is fine for new tables and useless for
reshaping a table that already holds data — so schema changes that touch live tables
live here instead, as explicit, ordered, idempotent steps.

Each migration is a module in this package named `mNNN_<slug>.py` exposing:

    VERSION     : str   -- "001", must be unique and sort in apply order
    DESCRIPTION : str
    def upgrade(conn) -> None   -- idempotent; safe to run twice

Applied versions are recorded in `schema_migrations`. `run_migrations()` applies only
what is missing, so it is safe to call on every startup (init_db does exactly that).

Deliberately not Alembic: Alembic wants to own table creation and needs the existing
database stamped as a baseline. The project already creates tables from the models, so
a runner that only carries *alterations* fits without restructuring the bootstrap. If
migrations ever outgrow this, Alembic can adopt the same version strings.
"""

import importlib
import logging
import pkgutil

from sqlalchemy import text

logger = logging.getLogger(__name__)

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     varchar PRIMARY KEY,
    description varchar,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def discover():
    """Return the migration modules in apply order.

    Returns:
        A list of imported modules sorted by their VERSION string.
    """
    mods = []
    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("m"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(mod, "VERSION") and hasattr(mod, "upgrade"):
            mods.append(mod)
    return sorted(mods, key=lambda m: m.VERSION)


def applied_versions(conn):
    """Return the set of migration versions already recorded as applied."""
    rows = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def run_migrations(engine):
    """Apply every migration that has not been applied yet.

    Each migration runs in its own transaction and is recorded on success, so a failure
    part-way through leaves earlier migrations applied and the failing one un-recorded.

    Args:
        engine: The SQLAlchemy engine to migrate.

    Returns:
        A list of the version strings applied by this call (empty when up to date).
    """
    with engine.begin() as conn:
        conn.execute(text(_TRACKING_TABLE))

    with engine.connect() as conn:
        done = applied_versions(conn)

    newly_applied = []
    for mod in discover():
        if mod.VERSION in done:
            logger.debug(f"migration {mod.VERSION} already applied — skipping")
            continue
        logger.info(f"Applying migration {mod.VERSION}: {mod.DESCRIPTION}")
        with engine.begin() as conn:
            mod.upgrade(conn)
            conn.execute(
                text("INSERT INTO schema_migrations (version, description) "
                     "VALUES (:v, :d) ON CONFLICT (version) DO NOTHING"),
                {"v": mod.VERSION, "d": mod.DESCRIPTION},
            )
        newly_applied.append(mod.VERSION)
        logger.info(f"Migration {mod.VERSION} applied.")

    if not newly_applied:
        logger.info("No pending migrations — schema is up to date.")
    return newly_applied
