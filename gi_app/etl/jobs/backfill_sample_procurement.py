"""Backfill `samples.days_to_sample_procurement` from GDC for samples already ingested.

Why this exists instead of a re-ingest
--------------------------------------
The column was added after the datasets were loaded (migration 002). A plain re-ingest would
repopulate it from `biospecimen/samples.tsv`, but only TCGA-COAD still has its raw TSVs on
disk: the "Add data" tool stages downloads in a temp directory it removes when the job ends,
so CHOL/LIHC/PAAD would need their slides re-downloaded just to recover one integer per row.
This job reads the field from the GDC cases endpoint and updates matched rows in place.

What the field means (it is NOT interchangeable with days_to_collection)
-----------------------------------------------------------------------
Per the GDC dictionary, `days_to_sample_procurement` is "the number of days from the index
date to the date a patient underwent a procedure (e.g. surgical resection) to yield or remove
from the patient a sample" — the clinical event. `days_to_collection` is "the number of days
… to the date a sample was received by the Biospecimen Core Resource (BCR) or other center
for processing" — a biobank accession date that routinely falls years later, and after death.
`case_timeline` uses the first for "Sample collected" and reports the second as an
administrative "Received by biobank" event.

Nothing is inferred: samples GDC does not date are left NULL and stay untimed in the view.
Matching is by sample barcode (`submitter_id`), which is unique in our schema.

Usage (from the api container):
    docker exec gi_app-api-1 python /etl/jobs/backfill_sample_procurement.py --dry-run
    docker exec gi_app-api-1 python /etl/jobs/backfill_sample_procurement.py
"""

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

GDC_CASES = "https://api.gdc.cancer.gov/cases"


def fetch_procurement_days(project):
    """Return {sample_barcode: days_to_sample_procurement} for one GDC project.

    Samples the source does not date are omitted, so callers can distinguish "GDC says
    nothing" from "GDC says 0" — 0 is a real value here and means procured on the index date.

    Args:
        project: GDC project id, e.g. "TCGA-COAD".

    Returns:
        A dict mapping sample barcode -> int day. May be empty.
    """
    filt = json.dumps({"op": "in", "content": {"field": "project.project_id", "value": [project]}})
    params = urllib.parse.urlencode({
        "filters": filt, "expand": "samples", "format": "JSON", "size": "5000",
    })
    with urllib.request.urlopen(f"{GDC_CASES}?{params}", timeout=180) as r:
        hits = json.loads(r.read())["data"]["hits"]

    out = {}
    for case in hits:
        for s in case.get("samples") or []:
            day, barcode = s.get("days_to_sample_procurement"), s.get("submitter_id")
            if barcode and day is not None:
                out[barcode] = int(day)
    return out


def resolve_projects(session):
    """Return the GDC project ids of the ingested datasets.

    Args:
        session: The active SQLAlchemy session.

    Returns:
        A sorted list of project id strings.
    """
    sys.path.insert(0, "/etl/connectors")
    import gdc_acquire

    from Database.models import Dataset

    projects = set()
    for ds in session.query(Dataset).all():
        project = gdc_acquire.resolve_project(ds.official_page or "")
        if project:
            projects.add(project)
    return sorted(projects)


def backfill(session, dry_run=False):
    """Populate days_to_sample_procurement for every ingested dataset.

    Args:
        session: The active SQLAlchemy session.
        dry_run: When True, report what would change and roll nothing forward.

    Returns:
        A dict of per-project {matched, updated} counts.
    """
    from sqlalchemy import text

    stats = {}
    for project in resolve_projects(session):
        days = fetch_procurement_days(project)
        logger.info(f"{project}: GDC dates {len(days)} samples")

        matched = updated = 0
        for barcode, day in days.items():
            row = session.execute(text(
                "SELECT days_to_sample_procurement FROM samples WHERE submitter_id = :b"
            ), {"b": barcode}).fetchone()
            if row is None:
                continue
            matched += 1
            if row[0] == day:
                continue
            if not dry_run:
                session.execute(text(
                    "UPDATE samples SET days_to_sample_procurement = :d WHERE submitter_id = :b"
                ), {"d": day, "b": barcode})
            updated += 1

        stats[project] = {"matched": matched, "updated": updated}
        logger.info(f"{project}: matched {matched} local samples, "
                    f"{'would update' if dry_run else 'updated'} {updated}")

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.path.insert(0, "/shared")
    from Database.database import SessionLocal

    session = SessionLocal()
    try:
        stats = backfill(session, dry_run=args.dry_run)
    finally:
        session.close()

    total = sum(s["updated"] for s in stats.values())
    print(f"\n{'would update' if args.dry_run else 'updated'} {total} samples "
          f"across {len(stats)} project(s)")


if __name__ == "__main__":
    main()
