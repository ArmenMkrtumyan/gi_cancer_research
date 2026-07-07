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
from Database.models import DataAsset, Dataset, Slide

logger = logging.getLogger(__name__)

DATASET_NAME = "TCGA-COAD"
DATASET_FOLDER = os.environ.get("TCGA_FOLDER", "TCGA_COAD")


def main():
    """Upload the sampled .svs slides to object storage and register them in data_assets.

    Returns:
        None.
    """
    session = SessionLocal()
    try:
        ds = session.query(Dataset).filter_by(name=DATASET_NAME).one_or_none()
        if ds is None:
            logger.error(f"Dataset {DATASET_NAME} not found — run tcga_ingest first.")
            return
        dataset_id = ds.dataset_id

        manifest = os.path.join(E.DATA_ROOT, DATASET_FOLDER, "slides_manifest.tsv")
        if not os.path.exists(manifest):
            logger.warning("slides_manifest.tsv not found — skipping slide ingest.")
            return

        # barcode -> slide_id (from the already-loaded slides table)
        smap = {s_id: sid for s_id, sid in
                session.query(Slide.submitter_id, Slide.slide_id).all()}

        # idempotent: clear this dataset's wsi assets, then re-register
        session.query(DataAsset).filter_by(dataset_id=dataset_id, asset_type="wsi").delete()
        session.commit()

        slides_dir = os.path.join(E.DATA_ROOT, DATASET_FOLDER, "slides")
        n_up, n_reg, missing = 0, 0, []
        for r in E.read_dicts(manifest):
            fname = E.s(r.get("file_name"))
            barcode = E.s(r.get("barcode"))
            if not fname:
                continue
            local = os.path.join(slides_dir, fname)
            if not os.path.exists(local):
                missing.append(fname)
                continue

            uri = storage.build_uri("bronze", "TCGA-COAD", "slides", fname)
            storage.put_file(local, uri)
            n_up += 1

            slide_id = smap.get(barcode)
            if slide_id is None:
                logger.warning(f"no slides row matches barcode {barcode} (asset still registered)")
            session.add(DataAsset(
                dataset_id=dataset_id, slide_id=slide_id, asset_type="wsi", layer="bronze",
                uri=uri, format="svs", md5=E.s(r.get("md5")),
                size_bytes=os.path.getsize(local), source_file_id=E.s(r.get("file_id")),
                created_at=datetime.now(timezone.utc),
            ))
            n_reg += 1

        session.commit()
        logger.info(f"slides: uploaded {n_up}, registered {n_reg} data_assets")
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
