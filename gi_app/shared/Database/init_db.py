"""Idempotent DB bootstrap: create all tables, then (re)create the two derived VIEWs.

Views (per schema.dbml — DBML can't model views, so they live here):
  - dataset_stats  : derived per-dataset counts (never stale).
  - case_survival  : OS-only survival. DSS/PFI/DFI need TCGA-CDR (later). Joins ONLY the
                     primary diagnosis so non-GI prior/synchronous primaries can't leak in.
"""

import logging

from sqlalchemy import text

from Database.database import Base, engine
from Database import models  # noqa: F401  (register all models on Base.metadata)

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


def init_db():
    """Create all tables (if missing) and (re)create the derived views. Idempotent.

    Returns:
        None.
    """
    logger.info("Creating tables (create_all, checkfirst=True)...")
    Base.metadata.create_all(engine)
    logger.info("Creating/refreshing views...")
    with engine.begin() as conn:
        conn.execute(text(DATASET_STATS_VIEW))
        conn.execute(text(CASE_SURVIVAL_VIEW))
    logger.info("Database initialized.")


if __name__ == "__main__":
    from logging_setup import configure_logging
    configure_logging()
    init_db()
