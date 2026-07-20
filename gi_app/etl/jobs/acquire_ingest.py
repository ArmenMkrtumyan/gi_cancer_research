#!/usr/bin/env python3
"""Orchestrate one download+ingest job for the "Add data" tool.

Runs as an isolated subprocess spawned by the API. Downloads a TCGA project to a temp dir,
ingests its clinical/biospecimen into Postgres and uploads the sampled slides to object
storage (local MinIO or AWS S3), updating the download_jobs row at each stage, then cleans up.

    python acquire_ingest.py --job-id 3 --project TCGA-STAD --name TCGA-STAD \\
        --cancer-types "Gastric adenocarcinoma" \\
        --page-url https://portal.gdc.cancer.gov/projects/TCGA-STAD --target local
"""

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

# DATA_ROOT must be set before etl_utils (imported transitively below) reads it at import.
_WORK = tempfile.mkdtemp(prefix="acquire-")
os.environ["DATA_ROOT"] = _WORK

# Put /etl, /etl/connectors, /etl/ingest on the path so the bare module names resolve
# (/shared + /etl are already on PYTHONPATH inside the container).
_ETL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "connectors", "ingest"):
    _p = os.path.join(_ETL, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gdc_acquire  # noqa: E402
import slide_ingest  # noqa: E402
import tcga_ingest  # noqa: E402
from Database.database import SessionLocal  # noqa: E402
from Database.models import DownloadJob  # noqa: E402
from logging_setup import configure_logging  # noqa: E402


def update_job(job_id, *, status=None, message=None, n_slides=None,
               bytes_done=None, bytes_total=None, finished=False):
    """Patch the download_jobs row for progress reporting."""
    session = SessionLocal()
    try:
        job = session.get(DownloadJob, job_id)
        if job is not None:
            if status is not None:
                job.status = status
            if message is not None:
                job.message = message
            if n_slides is not None:
                job.n_slides = n_slides
            if bytes_done is not None:
                job.bytes_done = bytes_done
            if bytes_total is not None:
                job.bytes_total = bytes_total
            if finished:
                job.finished_at = datetime.now(timezone.utc)
            session.commit()
    finally:
        session.close()


def main():
    p = argparse.ArgumentParser(description="Download + ingest one TCGA project (job runner).")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--project", required=True, help="GDC project id, e.g. TCGA-STAD")
    p.add_argument("--name", required=True, help="dataset name (datasets.name)")
    p.add_argument("--cancer-types", default=None)
    p.add_argument("--page-url", default=None)
    p.add_argument("--target", default="local", choices=["local", "aws"])
    p.add_argument("--limit", type=int, default=6, help="total slides to sample (0 = all / full)")
    args = p.parse_args()

    # Emit the ingest modules' INFO trace to this subprocess's stdout (captured in the
    # api container logs). Without this the root logger has no handlers and every
    # loader's progress line is silently dropped — leaving only the one-line failure
    # message on the download_jobs row to debug from.
    configure_logging()

    project = args.project
    page_url = args.page_url or f"https://portal.gdc.cancer.gov/projects/{project}"
    dest = os.path.join(_WORK, project)

    try:
        update_job(args.job_id, status="downloading", message="Querying GDC…")
        selected, _manifest = gdc_acquire.plan(project, dest, args.limit)
        update_job(args.job_id, n_slides=len(selected))

        def _progress(i, total, hit, file_done, file_size):
            # Report the specific file in flight (not an aggregate) so the UI mirrors the manifest.
            update_job(
                args.job_id,
                message=f"Slide {i} of {total}: {hit['file_name']}",
                bytes_done=file_done,
                bytes_total=file_size,
            )

        gdc_acquire.download(project, dest, selected, on_progress=_progress)

        update_job(args.job_id, status="ingesting", message="Loading clinical + biospecimen into Postgres…")
        tcga_ingest.main(
            dataset_name=args.name, project_id=project, page_url=page_url,
            cancer_types=args.cancer_types, folder=project, access="mixed",
        )
        slide_ingest.main(dataset_name=args.name, project_id=project, folder=project, target=args.target)

        update_job(args.job_id, status="done", message="Download and ingest complete.", finished=True)
    except Exception as exc:  # noqa: BLE001 — surface any failure to the job row
        update_job(args.job_id, status="failed", message=str(exc)[:1000], finished=True)
        raise
    finally:
        shutil.rmtree(_WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
