"""Idempotent DB bootstrap: migrate, create all tables, then (re)create the derived VIEWs.

Order matters. Migrations run FIRST because they reshape tables that already hold data
(`create_all` only ever creates missing tables — it never alters an existing one). On an
existing database migration 001 rebuilds `annotations` into its generalized shape; if
`create_all` ran first it would try to add `annotation_representations` with a foreign key
to the still-UUID `annotations.annotation_id` and fail on the type mismatch. On a fresh
database the migrations are no-ops and `create_all` builds the final shape from the models.

Views (per schema.dbml — DBML can't model views, so they live here):
  - dataset_stats  : derived per-dataset counts (never stale).
  - case_survival  : OS-only survival. DSS/PFI/DFI need TCGA-CDR (later). Joins ONLY the
                     primary diagnosis so non-GI prior/synchronous primaries can't leak in.
  - case_timeline  : longitudinal clinical events assembled from the source tables. A VIEW,
                     not a table — no clinical record is duplicated into an event store.
"""

import logging

from sqlalchemy import text

from Database.database import Base, engine
from Database import models  # noqa: F401  (register all models on Base.metadata)
from Database.migrations import run_migrations

logger = logging.getLogger(__name__)

DATASET_STATS_VIEW = """
CREATE OR REPLACE VIEW dataset_stats AS
SELECT d.dataset_id,
       COUNT(DISTINCT c.case_id)  AS n_cases,
       COUNT(DISTINCT a.asset_id) AS n_files
FROM datasets d
LEFT JOIN cases c       ON c.dataset_id = d.dataset_id
LEFT JOIN data_assets a ON a.dataset_id = d.dataset_id
GROUP BY d.dataset_id;
"""

CASE_SURVIVAL_VIEW = """
CREATE OR REPLACE VIEW case_survival AS
SELECT c.case_id,
       c.submitter_id,
       c.vital_status,
       COALESCE(c.days_to_death,
                MAX(d.days_to_last_follow_up),
                MAX(f.days_to_follow_up))        AS os_time,
       (c.vital_status = 'dead')::int            AS os_event
FROM cases c
LEFT JOIN diagnoses d  ON d.case_id = c.case_id
                      AND d.diagnosis_is_primary_disease = true   -- primary only
LEFT JOIN follow_ups f ON f.case_id = c.case_id
GROUP BY c.case_id, c.submitter_id, c.vital_status, c.days_to_death;
"""


# Longitudinal clinical timeline, assembled on read from the tables that already hold the
# data. Deliberately a VIEW: duplicating clinical records into a persistent event table
# would create a second source of truth that can drift from the first.
#
# Timing honesty rules encoded here:
#   * `day` is always days relative to the initial pathologic diagnosis — the unit TCGA
#     actually records. It is NEVER synthesized: a source NULL stays NULL.
#   * `timing_basis` states where the timing came from, so the UI can distinguish an
#     exact source value from a derived one from an unknown one.
#   * `created_datetime` is a record-keeping timestamp in GDC, not a clinical event date,
#     so it is never used as one. molecular_tests has no clinical date column at all and
#     is therefore reported with timing_basis='unknown' rather than given a fabricated day.
CASE_TIMELINE_VIEW = """
CREATE OR REPLACE VIEW case_timeline AS
-- Baseline: the initial primary diagnosis is day 0 by definition.
SELECT d.case_id, 'diagnosis'::varchar AS event_type, 0 AS day,
       'baseline'::varchar AS timing_basis,
       COALESCE(d.primary_diagnosis, 'Diagnosis')::varchar AS label,
       NULLIF(concat_ws(' · ', d.ajcc_pathologic_stage, d.tissue_or_organ_of_origin), '')::varchar AS detail,
       'diagnoses'::varchar AS ref_table, d.diagnosis_id::varchar AS ref_id,
       NULL::integer AS asset_id
FROM diagnoses d
WHERE d.diagnosis_is_primary_disease IS TRUE

UNION ALL
-- Additional (prior / synchronous) diagnoses: real records, but not placeable on the axis.
SELECT d.case_id, 'other_diagnosis', NULL, 'unknown',
       COALESCE(d.primary_diagnosis, 'Other diagnosis'),
       NULLIF(concat_ws(' · ', d.ajcc_pathologic_stage, d.classification_of_tumor), ''),
       'diagnoses', d.diagnosis_id::varchar, NULL
FROM diagnoses d
WHERE d.diagnosis_is_primary_disease IS DISTINCT FROM TRUE

UNION ALL
-- The clinical event: the day the tissue was taken from the patient.
SELECT s.case_id, 'sample_collection', s.days_to_sample_procurement, 'relative_to_diagnosis',
       COALESCE(s.sample_type, 'Sample collected'),
       NULLIF(concat_ws(' · ', s.submitter_id, s.tissue_type::text, s.preservation_method), ''),
       'samples', s.sample_id::varchar, NULL
FROM samples s
WHERE s.days_to_sample_procurement IS NOT NULL

UNION ALL
-- Administrative, NOT clinical: the day the biobank received the sample for processing.
-- Archived tissue is routinely accessioned years after diagnosis, so this legitimately falls
-- after death for many cases. Kept on the axis as a distinct event rather than being passed
-- off as a collection date (which is what it used to be labelled).
SELECT s.case_id, 'sample_received', s.days_to_collection, 'relative_to_diagnosis',
       'Received by biobank',
       NULLIF(concat_ws(' · ', s.submitter_id, s.sample_type), ''),
       'samples', s.sample_id::varchar, NULL
FROM samples s
WHERE s.days_to_collection IS NOT NULL

UNION ALL
SELECT t.case_id, 'treatment_start', t.days_to_treatment_start, 'relative_to_diagnosis',
       COALESCE(t.treatment_type, 'Treatment'),
       NULLIF(concat_ws(' · ', t.therapeutic_agents, t.treatment_intent_type), ''),
       'treatments', t.treatment_id::varchar, NULL
FROM treatments t
WHERE t.days_to_treatment_start IS NOT NULL

UNION ALL
SELECT t.case_id, 'treatment_end', t.days_to_treatment_end, 'relative_to_diagnosis',
       COALESCE(t.treatment_type, 'Treatment') || ' ended',
       t.treatment_outcome,
       'treatments', t.treatment_id::varchar, NULL
FROM treatments t
WHERE t.days_to_treatment_end IS NOT NULL

UNION ALL
SELECT f.case_id, 'follow_up', f.days_to_follow_up, 'relative_to_diagnosis',
       'Follow-up', f.disease_response,
       'follow_ups', f.follow_up_id::varchar, NULL
FROM follow_ups f
WHERE f.days_to_follow_up IS NOT NULL

UNION ALL
SELECT f.case_id, 'recurrence', f.days_to_recurrence, 'relative_to_diagnosis',
       'Recurrence',
       NULLIF(concat_ws(' · ', f.progression_or_recurrence_type,
                        f.progression_or_recurrence_anatomic_site), ''),
       'follow_ups', f.follow_up_id::varchar, NULL
FROM follow_ups f
WHERE f.days_to_recurrence IS NOT NULL

UNION ALL
SELECT f.case_id, 'progression', f.days_to_progression, 'relative_to_diagnosis',
       'Progression',
       NULLIF(concat_ws(' · ', f.progression_or_recurrence_type,
                        f.progression_or_recurrence_anatomic_site), ''),
       'follow_ups', f.follow_up_id::varchar, NULL
FROM follow_ups f
WHERE f.days_to_progression IS NOT NULL

UNION ALL
-- No clinical date exists for molecular tests in the source; reported as untimed.
SELECT m.case_id, 'molecular_test', NULL, 'unknown',
       COALESCE(m.laboratory_test, m.gene_symbol, 'Molecular test'),
       NULLIF(concat_ws(' · ', m.test_result, m.molecular_analysis_method,
                        m.timepoint_category), ''),
       'molecular_tests', m.molecular_test_id::varchar, NULL
FROM molecular_tests m

UNION ALL
-- Slide availability inherits its specimen's PROCUREMENT day; flagged as derived. It must not
-- fall back to days_to_collection: that is the biobank accession date, which would date the
-- slide to a shipment rather than to the operation that produced the tissue.
SELECT sl.case_id, 'slide_available', sa.days_to_sample_procurement,
       CASE WHEN sa.days_to_sample_procurement IS NULL THEN 'unknown' ELSE 'derived_from_specimen' END,
       sl.submitter_id, sl.slide_type,
       'slides', sl.slide_id::varchar, da.asset_id
FROM slides sl
JOIN data_assets da ON da.slide_id = sl.slide_id AND da.asset_type = 'wsi'
LEFT JOIN samples sa ON sa.sample_id = sl.sample_id

UNION ALL
SELECT c.case_id, 'death', c.days_to_death, 'relative_to_diagnosis',
       'Death', NULL, 'cases', c.case_id::varchar, NULL
FROM cases c
WHERE c.days_to_death IS NOT NULL

UNION ALL
SELECT d.case_id, 'last_follow_up', MAX(d.days_to_last_follow_up), 'relative_to_diagnosis',
       'Last known follow-up', NULL, 'diagnoses', MIN(d.diagnosis_id::varchar), NULL
FROM diagnoses d
WHERE d.days_to_last_follow_up IS NOT NULL
GROUP BY d.case_id;
"""


# The 7 TCGA GI-cancer projects (Data.xlsx rows 1-7). Seeded into the download registry so
# the "Add data" page is useful on day one. GEO rows are deferred (no connector yet).
SEED_CATALOG = [
    ("TCGA-COAD", "Colon adenocarcinoma"),
    ("TCGA-READ", "Rectal adenocarcinoma"),
    ("TCGA-STAD", "Gastric adenocarcinoma"),
    ("TCGA-ESCA", "Esophageal carcinoma"),
    ("TCGA-PAAD", "Pancreatic ductal adenocarcinoma"),
    ("TCGA-LIHC", "Liver hepatocellular carcinoma"),
    ("TCGA-CHOL", "Cholangiocarcinoma"),
]


def seed_download_catalog():
    """Seed the 7 TCGA projects into download_catalog (idempotent, keyed by source_url)."""
    from datetime import datetime, timezone

    from Database.database import SessionLocal
    from Database.models import DownloadCatalog

    session = SessionLocal()
    try:
        existing = {r[0] for r in session.query(DownloadCatalog.source_url).all()}
        added = 0
        for project, cancer in SEED_CATALOG:
            url = f"https://portal.gdc.cancer.gov/projects/{project}"
            if url in existing:
                continue
            session.add(DownloadCatalog(
                name=project, source_url=url, source_type="gdc",
                gi_cancer_types=cancer, created_at=datetime.now(timezone.utc),
            ))
            added += 1
        session.commit()
        if added:
            logger.info(f"Seeded {added} TCGA projects into download_catalog.")
    finally:
        session.close()


def init_db():
    """Create all tables (if missing), (re)create the derived views, seed the catalog. Idempotent.

    Returns:
        None.
    """
    logger.info("Running pending migrations...")
    # Before create_all: migrations reshape tables that already exist and hold data.
    run_migrations(engine)
    logger.info("Creating tables (create_all, checkfirst=True)...")
    Base.metadata.create_all(engine)
    logger.info("Creating/refreshing views...")
    with engine.begin() as conn:
        conn.execute(text(DATASET_STATS_VIEW))
        conn.execute(text(CASE_SURVIVAL_VIEW))
        conn.execute(text(CASE_TIMELINE_VIEW))
    seed_download_catalog()
    logger.info("Database initialized.")


if __name__ == "__main__":
    from logging_setup import configure_logging
    configure_logging()
    init_db()
