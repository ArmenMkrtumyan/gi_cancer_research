"""Upload the pathology-report PDFs to object storage and register them in `data_assets`,
linked to their `cases` row by barcode.

Reads reports_manifest.tsv (written by gdc_reports) for the file list + md5 + source file
id; the byte size comes from the file on disk. Deliberately parallel to slide_ingest, with
one difference: a report is case-scoped, so it sets `case_id` where a slide sets `slide_id`.

Text extraction is a separate step. This module only makes the source document available —
what the report *says* is not touched here.
"""

import logging
import os
from datetime import datetime, timezone

import etl_utils as E
import storage
from Database.database import SessionLocal
from Database.models import Case, DataAsset, Dataset

logger = logging.getLogger(__name__)

DATASET_NAME = "TCGA-COAD"
DATASET_FOLDER = os.environ.get("TCGA_FOLDER", "TCGA_COAD")


def main(dataset_name=DATASET_NAME, project_id="TCGA-COAD", folder=None, target="local"):
    """Upload the report PDFs to object storage and register them in data_assets.

    Args:
        dataset_name: The ingested dataset to attach the assets to.
        project_id: GDC project id; used as the bronze URI path segment.
        folder: Source folder under DATA_ROOT (defaults to env TCGA_FOLDER).
        target: Storage target ("local" MinIO | "aws" S3).

    Returns:
        None.
    """
    global DATASET_FOLDER
    if folder:
        DATASET_FOLDER = folder
    session = SessionLocal()
    try:
        ds = session.query(Dataset).filter_by(name=dataset_name).one_or_none()
        if ds is None:
            logger.error(f"Dataset {dataset_name} not found — run tcga_ingest first.")
            return
        dataset_id = ds.dataset_id

        reports_dir = os.path.join(E.DATA_ROOT, DATASET_FOLDER, "pathology_reports")
        manifest = os.path.join(reports_dir, "reports_manifest.tsv")
        if not os.path.exists(manifest):
            logger.warning("reports_manifest.tsv not found — skipping report ingest.")
            return

        # barcode -> case_id, scoped to this dataset: barcodes are unique per project, and
        # an unscoped lookup would attach a report to a same-barcode case in another one.
        cmap = {sid: cid for sid, cid in
                session.query(Case.submitter_id, Case.case_id)
                .filter(Case.dataset_id == dataset_id).all()}

        # Idempotent by URI rather than delete-and-reinsert, so a re-run does not hand the
        # same PDF a new asset_id (anything that later references a report keeps working).
        existing = {a.uri: a for a in session.query(DataAsset)
                    .filter_by(dataset_id=dataset_id, asset_type="pathology_report").all()}

        n_up, n_new, n_upd, missing, unmatched = 0, 0, 0, [], []
        # Two sets, deliberately: `in_manifest` is what the dataset *claims*, `seen` is what
        # this run actually uploaded. Retirement keys off the first. Keying it off the second
        # would mean a machine that has the manifest but not the PDFs (they are only needed
        # for the upload, and 42 MB is easy to clear) deregisters every report while the
        # objects stay in storage — silent data loss dressed up as a clean re-run.
        in_manifest, seen = set(), set()
        for r in E.read_dicts(manifest):
            fname = E.s(r.get("file_name"))
            barcode = E.s(r.get("case"))
            if not fname:
                continue
            local = os.path.join(reports_dir, fname)
            uri_claimed = storage.build_uri("bronze", project_id, "pathology_reports",
                                            fname, target=target)
            in_manifest.add(uri_claimed)
            if not os.path.exists(local):
                missing.append(fname)
                continue

            case_id = cmap.get(barcode)
            if case_id is None:
                # No case row means the report belongs to a patient this dataset never
                # ingested. Registering it would create an asset nothing can reach, so skip.
                unmatched.append(barcode)
                continue

            uri = uri_claimed
            storage.put_file(local, uri, target=target)
            n_up += 1
            seen.add(uri)

            fields = dict(
                dataset_id=dataset_id, case_id=case_id, asset_type="pathology_report",
                layer="bronze", format="pdf", md5=E.s(r.get("md5")),
                size_bytes=os.path.getsize(local), source_file_id=E.s(r.get("file_id")),
            )
            asset = existing.get(uri)
            if asset is None:
                session.add(DataAsset(uri=uri, created_at=datetime.now(timezone.utc), **fields))
                n_new += 1
            else:
                for k, v in fields.items():
                    setattr(asset, k, v)
                n_upd += 1

        # Retire only what the manifest has genuinely dropped — a report the dataset no
        # longer contains. Unlike slides there is nothing pointing at a report yet, so no
        # reference check is needed here; add one if that changes.
        n_retired = 0
        for uri, a in existing.items():
            if uri not in in_manifest:
                session.delete(a)
                n_retired += 1

        session.commit()
        logger.info(f"pathology reports: uploaded {n_up}, registered {n_new} new + {n_upd} updated "
                    f"data_assets" + (f", retired {n_retired} dropped from the manifest" if n_retired else ""))
        if unmatched:
            logger.warning(f"{len(unmatched)} report(s) had no matching case in {dataset_name} "
                           f"(skipped): {unmatched[:10]}{' …' if len(unmatched) > 10 else ''}")
        if missing:
            logger.warning(f"{len(missing)} report file(s) listed in manifest but missing on disk: "
                           f"{missing[:10]}{' …' if len(missing) > 10 else ''}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    from logging_setup import configure_logging
    configure_logging()
    main()
