"""Upload the sampled .svs whole-slide images to object storage and register them in
`data_assets`, linked to their `slides` row by barcode.

Reads slides_manifest.tsv (written by gdc_acquire) for the file list + md5 + source
file id; the byte size comes from the file on disk. OpenSlide thumbnails are Phase 2.
"""

import logging
import os
from datetime import datetime, timezone

import etl_utils as E
import storage
from Database.database import SessionLocal
from Database.models import Annotation, DataAsset, Dataset, Slide
from ingest.tcga_ingest import relink_published_annotations

logger = logging.getLogger(__name__)

DATASET_NAME = "TCGA-COAD"
DATASET_FOLDER = os.environ.get("TCGA_FOLDER", "TCGA_COAD")


def main(dataset_name=DATASET_NAME, project_id="TCGA-COAD", folder=None, target="local"):
    """Upload the sampled .svs slides to object storage and register them in data_assets.

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

        manifest = os.path.join(E.DATA_ROOT, DATASET_FOLDER, "slides_manifest.tsv")
        if not os.path.exists(manifest):
            logger.warning("slides_manifest.tsv not found — skipping slide ingest.")
            return

        # barcode -> slide_id (from the already-loaded slides table)
        smap = {s_id: sid for s_id, sid in
                session.query(Slide.submitter_id, Slide.slide_id).all()}

        # Idempotent by URI rather than delete-and-reinsert: spatial annotations target a
        # slide by asset_id, so re-running the ETL must not hand the same file a new id.
        existing = {a.uri: a for a in session.query(DataAsset)
                    .filter_by(dataset_id=dataset_id, asset_type="wsi").all()}

        slides_dir = os.path.join(E.DATA_ROOT, DATASET_FOLDER, "slides")
        n_up, n_new, n_upd, missing = 0, 0, 0, []
        seen = set()
        for r in E.read_dicts(manifest):
            fname = E.s(r.get("file_name"))
            barcode = E.s(r.get("barcode"))
            if not fname:
                continue
            local = os.path.join(slides_dir, fname)
            if not os.path.exists(local):
                missing.append(fname)
                continue

            uri = storage.build_uri("bronze", project_id, "slides", fname, target=target)
            storage.put_file(local, uri, target=target)
            n_up += 1
            seen.add(uri)

            slide_id = smap.get(barcode)
            if slide_id is None:
                logger.warning(f"no slides row matches barcode {barcode} (asset still registered)")
            fields = dict(
                dataset_id=dataset_id, slide_id=slide_id, asset_type="wsi", layer="bronze",
                format="svs", md5=E.s(r.get("md5")), size_bytes=os.path.getsize(local),
                source_file_id=E.s(r.get("file_id")),
            )
            asset = existing.get(uri)
            if asset is None:
                session.add(DataAsset(uri=uri, created_at=datetime.now(timezone.utc), **fields))
                n_new += 1
            else:
                for k, v in fields.items():
                    setattr(asset, k, v)
                n_upd += 1

        # Retire assets no longer in the manifest, unless an annotation still points at one
        # (dropping those would silently orphan published spatial work).
        stale = [a for uri, a in existing.items() if uri not in seen]
        kept = 0
        for a in stale:
            refs = session.query(Annotation).filter_by(target_asset_id=a.asset_id).count()
            if refs:
                kept += 1
                continue
            session.delete(a)

        session.commit()
        logger.info(f"slides: uploaded {n_up}, registered {n_new} new + {n_upd} updated data_assets"
                    + (f", kept {kept} stale asset(s) still referenced by annotations" if kept else ""))

        # Restoring asset -> slide linkage is what makes a published annotation's case
        # resolvable again, so the re-link belongs here rather than in tcga_ingest (which
        # runs before these links exist).
        relink_published_annotations(session, dataset_id)
        if missing:
            logger.warning(f"{len(missing)} slide files listed in manifest but missing on disk: {missing}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    from logging_setup import configure_logging
    configure_logging()
    main()
