"""002 — add `samples.days_to_sample_procurement`.

The timeline previously drew its "Sample collected" event from `samples.days_to_collection`.
That field does not mean what the label claimed. Per the GDC dictionary it is "the number of
days from the index date to the date a sample was received by the Biospecimen Core Resource
(BCR) or other center for processing" — a biobank *accession* date. The clinical event, "the
date a patient underwent a procedure … to yield or remove from the patient a sample", is
`days_to_sample_procurement`, which was never ingested.

The consequence was visible: patients showed samples "collected" hundreds of days after their
recorded death (176 of 250 cases with both dates). That is not a source error — archived
tissue reaches a biobank long after diagnosis, and often after the patient has died — it was a
mislabelled event.

This migration only adds the column. Backfilling it needs the source TSVs, so it is left to
the ETL: `samples.tsv` already carries `days_to_sample_procurement`, so a re-ingest populates
it with no re-download. Until then the column is NULL and the view reports those samples as
untimed, which is the honest state.

Idempotent: `ADD COLUMN IF NOT EXISTS`. On a fresh database `create_all` builds the column
from the model and this is a no-op.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

VERSION = "002"
DESCRIPTION = "Add samples.days_to_sample_procurement (clinical collection day)"


def _table_exists(conn, table):
    return conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:t"
    ), {"t": table}).scalar() is not None


def upgrade(conn):
    """Add the procurement-day column to an existing `samples` table."""
    if not _table_exists(conn, "samples"):
        logger.info("002: samples table absent; create_all will build the final shape")
        return

    conn.execute(text(
        "ALTER TABLE samples ADD COLUMN IF NOT EXISTS days_to_sample_procurement integer"
    ))
    logger.info("002: samples.days_to_sample_procurement present (re-ingest to populate)")
