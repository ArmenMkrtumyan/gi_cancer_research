"""001 — generalize `annotations` into a normalized, provenance-carrying annotation system.

Reshapes an EXISTING database in place, preserving every GDC annotation row:

  * `annotation_sets`            (new)  — one row per imported collection/release.
  * `annotations`                (alter) — surrogate PK; the GDC UUID moves to
                                  `source_annotation_id`; entity/date columns are
                                  renamed to their `source_*` names; the descriptive
                                  and spatial-targeting columns are added.
  * `annotation_representations` (new)  — file-backed representations of spatial rows.
  * `data_assets`                (alter) — `asset_type` enum -> varchar + CHECK, plus
                                  external-provenance and derived-from columns.

No row is deleted and no table is dropped or recreated. On a fresh database (where
`annotations` does not exist yet) this is a no-op: `create_all` builds the final shape
directly from the models. Re-running is safe — every step is guarded.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

VERSION = "001"
DESCRIPTION = "Generalize annotations: annotation_sets + spatial targeting + representations"

GDC_SET_NAME_SQL = "'GDC Annotations — ' || d.name"
GDC_CITATION = (
    "Genomic Data Commons Data Portal, National Cancer Institute. "
    "Annotations are administrative/QC notes recorded by the data-coordinating centre."
)


def _table_exists(conn, table):
    return conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:t"
    ), {"t": table}).scalar() is not None


def _column_exists(conn, table, column):
    return conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
    ), {"t": table, "c": column}).scalar() is not None


def _constraint_exists(conn, name):
    return conn.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname=:n"
    ), {"n": name}).scalar() is not None


def _create_annotation_sets(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS annotation_sets (
            annotation_set_id serial PRIMARY KEY,
            dataset_id        integer NOT NULL REFERENCES datasets(dataset_id),
            name              varchar NOT NULL,
            provider          varchar,
            source_url        varchar,
            citation          text,
            license           varchar,
            version           varchar NOT NULL DEFAULT '1',
            method            varchar,
            origin            varchar NOT NULL,
            description       text,
            retrieved_at      timestamptz,
            created_at        timestamptz,
            CONSTRAINT uq_annotation_sets_dataset_name_version UNIQUE (dataset_id, name, version),
            CONSTRAINT ck_annotation_sets_origin
                CHECK (origin IN ('source_provided', 'published_derived')),
            CONSTRAINT ck_annotation_sets_method
                CHECK (method IN ('manual', 'algorithmic', 'mixed', 'not_reported'))
        );
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_annotation_sets_dataset_id "
        "ON annotation_sets (dataset_id);"
    ))


def _alter_data_assets(conn):
    """asset_type enum -> varchar + CHECK; add external provenance + derivative link."""
    if not _table_exists(conn, "data_assets"):
        return
    # The enum only ever held 'wsi'; widening to varchar lets new asset types land by
    # plain migration instead of ALTER TYPE ... ADD VALUE (which cannot be used in the
    # same transaction that inserts the new value).
    is_enum = conn.execute(text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='data_assets'
          AND column_name='asset_type' AND data_type='USER-DEFINED'
    """)).scalar()
    if is_enum:
        conn.execute(text(
            "ALTER TABLE data_assets ALTER COLUMN asset_type TYPE varchar "
            "USING asset_type::text;"
        ))
        logger.info("data_assets.asset_type: enum -> varchar")

    if not _column_exists(conn, "data_assets", "source_uri"):
        conn.execute(text("ALTER TABLE data_assets ADD COLUMN source_uri varchar;"))
    if not _column_exists(conn, "data_assets", "derived_from_asset_id"):
        conn.execute(text(
            "ALTER TABLE data_assets ADD COLUMN derived_from_asset_id integer "
            "REFERENCES data_assets(asset_id);"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_data_assets_derived_from_asset_id "
            "ON data_assets (derived_from_asset_id);"
        ))
    if not _constraint_exists(conn, "ck_data_assets_asset_type"):
        conn.execute(text("""
            ALTER TABLE data_assets ADD CONSTRAINT ck_data_assets_asset_type
            CHECK (asset_type IN ('wsi', 'annotation_source', 'annotation_mask',
                                  'annotation_vector', 'rendering_cache'));
        """))

    # The old enum type is now unreferenced.
    still_used = conn.execute(text("""
        SELECT 1 FROM pg_attribute a
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE t.typname = 'asset_type' AND a.attisdropped = false
    """)).scalar()
    if not still_used:
        conn.execute(text("DROP TYPE IF EXISTS asset_type;"))


def _reshape_annotations(conn):
    """Move the GDC UUID to source_annotation_id and add the generalized columns."""
    already = _column_exists(conn, "annotations", "source_annotation_id")
    if already:
        logger.info("annotations already reshaped — skipping column surgery")
    else:
        before = conn.execute(text("SELECT count(*) FROM annotations")).scalar()
        logger.info(f"annotations rows before reshape: {before}")

        pkey = conn.execute(text("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'annotations'::regclass AND contype = 'p'
        """)).scalar()
        if pkey:
            conn.execute(text(f'ALTER TABLE annotations DROP CONSTRAINT "{pkey}";'))

        # Rename in place: the GDC UUID *becomes* source_annotation_id, so no row is
        # copied and nothing can be lost in transit.
        conn.execute(text("ALTER TABLE annotations RENAME COLUMN annotation_id TO source_annotation_id;"))
        conn.execute(text(
            "ALTER TABLE annotations ALTER COLUMN source_annotation_id TYPE varchar "
            "USING source_annotation_id::text;"
        ))
        conn.execute(text("ALTER TABLE annotations ALTER COLUMN source_annotation_id SET NOT NULL;"))

        for old, new in (("entity_type", "source_entity_type"),
                         ("entity_submitter_id", "source_entity_submitter_id"),
                         ("created_datetime", "source_created_datetime")):
            if _column_exists(conn, "annotations", old):
                conn.execute(text(f"ALTER TABLE annotations RENAME COLUMN {old} TO {new};"))
        # Spatial rows have no GDC barcode.
        conn.execute(text(
            "ALTER TABLE annotations ALTER COLUMN source_entity_submitter_id DROP NOT NULL;"
        ))

        conn.execute(text("ALTER TABLE annotations ADD COLUMN annotation_id serial;"))
        conn.execute(text("ALTER TABLE annotations ADD PRIMARY KEY (annotation_id);"))

        after = conn.execute(text("SELECT count(*) FROM annotations")).scalar()
        if after != before:
            raise RuntimeError(f"annotation count changed during reshape: {before} -> {after}")
        logger.info(f"annotations rows after reshape: {after} (unchanged)")

    for ddl in (
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS annotation_set_id integer",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS target_asset_id integer",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS scope varchar",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS annotation_type varchar",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS label varchar",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS value_text text",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS value_number double precision",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS units varchar",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS confidence double precision",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS review_status varchar",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS source_updated_datetime timestamptz",
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS created_at timestamptz",
    ):
        conn.execute(text(ddl + ";"))


def _backfill_gdc_sets(conn):
    """Create one GDC annotation set per dataset and attach the existing rows to it."""
    unassigned = conn.execute(text(
        "SELECT count(*) FROM annotations WHERE annotation_set_id IS NULL"
    )).scalar()
    if not unassigned:
        return

    orphans = conn.execute(text(
        "SELECT count(*) FROM annotations WHERE annotation_set_id IS NULL AND case_id IS NULL"
    )).scalar()
    if orphans:
        raise RuntimeError(
            f"{orphans} pre-existing annotations have no case_id, so their dataset (and "
            "therefore their annotation set) cannot be resolved. Resolve these manually "
            "before migrating rather than losing their provenance."
        )

    conn.execute(text(f"""
        INSERT INTO annotation_sets
            (dataset_id, name, provider, source_url, citation, license, version,
             method, origin, description, created_at)
        SELECT DISTINCT c.dataset_id,
               {GDC_SET_NAME_SQL},
               'GDC (Genomic Data Commons)',
               'https://portal.gdc.cancer.gov/annotations',
               :citation,
               'NCI GDC Data Portal terms of use',
               '1',
               'manual',
               'source_provided',
               'Administrative and quality-control notes recorded against GDC entities. '
               'Non-spatial: these carry no slide coordinates, polygons or masks.',
               now()
        FROM annotations a
        JOIN cases c    ON c.case_id = a.case_id
        JOIN datasets d ON d.dataset_id = c.dataset_id
        WHERE a.annotation_set_id IS NULL
        ON CONFLICT (dataset_id, name, version) DO NOTHING;
    """), {"citation": GDC_CITATION})

    conn.execute(text(f"""
        UPDATE annotations a
        SET annotation_set_id = s.annotation_set_id
        FROM cases c
        JOIN datasets d       ON d.dataset_id = c.dataset_id
        JOIN annotation_sets s ON s.dataset_id = c.dataset_id
                              AND s.name = {GDC_SET_NAME_SQL}
                              AND s.version = '1'
        WHERE a.case_id = c.case_id AND a.annotation_set_id IS NULL;
    """))

    # GDC annotations are administrative notes about an entity, never slide regions.
    conn.execute(text("""
        UPDATE annotations
        SET scope = COALESCE(scope, 'case'),
            annotation_type = COALESCE(annotation_type, 'qc'),
            label = COALESCE(label, category),
            created_at = COALESCE(created_at, now())
        WHERE annotation_type IS NULL OR scope IS NULL;
    """))

    left = conn.execute(text(
        "SELECT count(*) FROM annotations WHERE annotation_set_id IS NULL"
    )).scalar()
    if left:
        raise RuntimeError(f"{left} annotations could not be assigned an annotation set")
    logger.info(f"Backfilled {unassigned} GDC annotations into per-dataset annotation sets.")


def _finalize_annotation_constraints(conn):
    conn.execute(text("ALTER TABLE annotations ALTER COLUMN scope SET DEFAULT 'case';"))
    conn.execute(text("UPDATE annotations SET scope = 'case' WHERE scope IS NULL;"))
    for col in ("annotation_set_id", "scope"):
        conn.execute(text(f"ALTER TABLE annotations ALTER COLUMN {col} SET NOT NULL;"))

    if not _constraint_exists(conn, "annotations_annotation_set_id_fkey"):
        conn.execute(text("""
            ALTER TABLE annotations ADD CONSTRAINT annotations_annotation_set_id_fkey
            FOREIGN KEY (annotation_set_id) REFERENCES annotation_sets(annotation_set_id);
        """))
    if not _constraint_exists(conn, "annotations_target_asset_id_fkey"):
        conn.execute(text("""
            ALTER TABLE annotations ADD CONSTRAINT annotations_target_asset_id_fkey
            FOREIGN KEY (target_asset_id) REFERENCES data_assets(asset_id);
        """))
    if not _constraint_exists(conn, "uq_annotations_set_source_id"):
        conn.execute(text("""
            ALTER TABLE annotations ADD CONSTRAINT uq_annotations_set_source_id
            UNIQUE (annotation_set_id, source_annotation_id);
        """))
    if not _constraint_exists(conn, "ck_annotations_scope"):
        conn.execute(text("""
            ALTER TABLE annotations ADD CONSTRAINT ck_annotations_scope
            CHECK (scope IN ('case', 'slide', 'region', 'nucleus', 'patch', 'tile'));
        """))
    if not _constraint_exists(conn, "ck_annotations_spatial_needs_asset"):
        conn.execute(text("""
            ALTER TABLE annotations ADD CONSTRAINT ck_annotations_spatial_needs_asset
            CHECK (scope NOT IN ('region', 'nucleus', 'patch', 'tile')
                   OR target_asset_id IS NOT NULL);
        """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_annotations_annotation_set_id ON annotations (annotation_set_id)",
        "CREATE INDEX IF NOT EXISTS ix_annotations_target_asset_id ON annotations (target_asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_annotations_case_id ON annotations (case_id)",
        "CREATE INDEX IF NOT EXISTS ix_annotations_source_entity_submitter_id "
        "ON annotations (source_entity_submitter_id)",
        "CREATE INDEX IF NOT EXISTS ix_annotations_set_scope ON annotations (annotation_set_id, scope)",
    ):
        conn.execute(text(stmt + ";"))


def _create_representations(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS annotation_representations (
            representation_id   serial PRIMARY KEY,
            annotation_id       integer NOT NULL REFERENCES annotations(annotation_id),
            asset_id            integer NOT NULL REFERENCES data_assets(asset_id),
            representation_type varchar,
            coordinate_space    varchar,
            width               integer,
            height              integer,
            level               integer,
            transform_metadata  jsonb,
            minimum_value       double precision,
            maximum_value       double precision,
            created_at          timestamptz,
            CONSTRAINT uq_annotation_reps_annotation_asset UNIQUE (annotation_id, asset_id)
        );
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_annotation_representations_annotation_id "
        "ON annotation_representations (annotation_id)",
        "CREATE INDEX IF NOT EXISTS ix_annotation_representations_asset_id "
        "ON annotation_representations (asset_id)",
    ):
        conn.execute(text(stmt + ";"))


def upgrade(conn):
    """Apply migration 001. Idempotent.

    Args:
        conn: An open SQLAlchemy connection inside a transaction.

    Returns:
        None.
    """
    if not _table_exists(conn, "annotations"):
        logger.info("Fresh database — create_all will build the final shape; nothing to migrate.")
        return

    _create_annotation_sets(conn)
    _alter_data_assets(conn)
    _reshape_annotations(conn)
    _backfill_gdc_sets(conn)
    _finalize_annotation_constraints(conn)
    _create_representations(conn)
