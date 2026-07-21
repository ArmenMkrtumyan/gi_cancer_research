"""003 — let `data_assets` hold case-scoped pathology reports.

Two changes, both needed before the report PDFs can be registered:

1. `asset_type` gains 'pathology_report'. The column is varchar + CHECK (not a Postgres
   enum) precisely so a new type is a plain constraint swap; see DataAsset's docstring.

2. `data_assets` gains `case_id`. Every asset type registered so far hangs off a *slide*
   (`wsi`, and the annotation masks that target one), so `slide_id` was the only link the
   table needed. A pathology report is not a slide artefact — it describes the specimen a
   patient's resection produced, and GDC publishes it per case. Without a case link there
   is no way to answer "show me this patient's report", which is the entire point.

`slide_id` and `case_id` are both nullable and neither is exclusive: a wsi sets slide_id,
a pathology_report sets case_id, and a future per-slide derivative could set both. The
report's own `pathology_report_uuid` (the tag embedded in the PDF filename, already loaded
onto `samples`) is kept in `source_file_id`/`uri` rather than given a column — it is
provenance, not a key we join on.

Idempotent: ADD COLUMN IF NOT EXISTS, and the CHECK is dropped before being recreated. On
a fresh database `create_all` builds the final shape from the models and this is a no-op.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

VERSION = "003"
DESCRIPTION = "data_assets: add case_id + allow asset_type='pathology_report'"

# Kept as a literal rather than imported from models.ASSET_TYPES: a migration records the
# shape the schema had at *this* version, so a later edit to the model tuple must not
# silently rewrite what 003 did.
ASSET_TYPES_003 = (
    "wsi", "annotation_source", "annotation_mask", "annotation_vector",
    "rendering_cache", "pathology_report",
)


def _table_exists(conn, table):
    return conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:t"
    ), {"t": table}).scalar() is not None


def upgrade(conn):
    """Add data_assets.case_id and widen the asset_type CHECK."""
    if not _table_exists(conn, "data_assets"):
        logger.info("003: data_assets absent; create_all will build the final shape")
        return

    conn.execute(text(
        "ALTER TABLE data_assets ADD COLUMN IF NOT EXISTS case_id uuid"
    ))

    # The FK is added separately and guarded: on a database where `cases` somehow does not
    # exist yet the column is still useful, and re-running must not fail on a dup name.
    if _table_exists(conn, "cases"):
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'fk_data_assets_case_id'
                ) THEN
                    ALTER TABLE data_assets
                        ADD CONSTRAINT fk_data_assets_case_id
                        FOREIGN KEY (case_id) REFERENCES cases (case_id);
                END IF;
            END $$;
        """))

    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_data_assets_case_id ON data_assets (case_id)"
    ))

    conn.execute(text("ALTER TABLE data_assets DROP CONSTRAINT IF EXISTS ck_data_assets_asset_type"))
    conn.execute(text(
        "ALTER TABLE data_assets ADD CONSTRAINT ck_data_assets_asset_type "
        f"CHECK (asset_type IN {ASSET_TYPES_003})"
    ))

    logger.info("003: data_assets.case_id present; asset_type now allows 'pathology_report'")
