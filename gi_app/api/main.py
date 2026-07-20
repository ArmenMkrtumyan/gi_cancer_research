"""FastAPI app for the Phase-1 ingested-data dashboard.

This first web-tool pass intentionally focuses on data that already exists in
Postgres/object storage. Future catalog/download-planning endpoints can sit next
to these once we bring Data.xlsx into the app.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from math import ceil
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import distinct, func, text
from sqlalchemy.orm import Session

import storage
import wsi
from Database.database import get_db, SessionLocal
from Database.models import (
    Aliquot,
    Analyte,
    Annotation,
    AnnotationRepresentation,
    AnnotationSet,
    Case,
    DataAsset,
    Dataset,
    Diagnosis,
    DownloadCatalog,
    DownloadJob,
    FollowUp,
    IngestionRun,
    MolecularTest,
    PathologyDetail,
    Portion,
    Sample,
    Slide,
    Treatment,
)
from Database.utils import detect_source_type

# Make the acquisition library importable (it lives under the mounted /etl tree).
_ETL_CONNECTORS = "/etl/connectors"
if os.path.isdir(_ETL_CONNECTORS) and _ETL_CONNECTORS not in sys.path:
    sys.path.insert(0, _ETL_CONNECTORS)

app = FastAPI(title="GI Cancer Research API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _reconcile_orphaned_jobs() -> None:
    """Fail any download job left mid-flight by a server restart.

    A download/ingest runs in a subprocess that is a child of THIS API process, so an API
    restart (redeploy, crash, `docker compose up` touching the api container) kills it
    silently. The job row would otherwise stay stuck at 'downloading'/'ingesting' forever
    and the UI would poll a dead job. Reaching this startup hook proves that child is gone,
    so flip any still-active job to 'failed' with a clear reason.
    """
    session = SessionLocal()
    try:
        stale = (
            session.query(DownloadJob)
            .filter(DownloadJob.status.in_(["pending", "downloading", "ingesting"]))
            .all()
        )
        for job in stale:
            job.status = "failed"
            job.message = "Interrupted: the server restarted while this job was running. Please re-download."
            job.finished_at = datetime.now(timezone.utc)
        if stale:
            session.commit()
            print(f"[startup] Reconciled {len(stale)} orphaned download job(s) -> failed.")
    finally:
        session.close()


def _value(v):
    """Return a JSON-friendly value for SQLAlchemy enum/string/date-ish values."""
    return getattr(v, "value", v)


def _count(v) -> int:
    """Normalize SQL count/sum scalars."""
    return int(v or 0)


def _pct(part: int, total: int) -> Optional[float]:
    """Return a rounded percentage, or None when the denominator is zero."""
    if not total:
        return None
    return round((part / total) * 100, 1)


def _filename(uri: Optional[str]) -> Optional[str]:
    """Extract a display filename from an object-storage URI."""
    if not uri:
        return None
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _dataset_or_404(db: Session, dataset_id: int) -> Dataset:
    """Fetch a dataset or raise a 404."""
    ds = db.query(Dataset).filter(Dataset.dataset_id == dataset_id).one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found")
    return ds


# child->parent delete order for a dataset's case-scoped rows (mirrors
# tcga_ingest._RESET_CASE_SCOPED). data_assets + ingestion_runs + cases are cleared
# separately; download_catalog/download_jobs are NOT touched (they're the wishlist/audit log).
_CASE_SCOPED_TABLES = [
    "aliquots", "analytes", "portions", "slides",
    "molecular_tests", "follow_ups", "pathology_details", "treatments",
    "samples", "diagnoses",
]


def _purge_dataset(db: Session, ds: Dataset) -> dict:
    """Delete an ingested dataset entirely: its stored slide objects, then all of its
    Postgres rows (data_assets, case-scoped children, cases, ingestion_runs, the dataset).

    Storage deletion is best-effort and runs first so a stuck object never blocks freeing
    the database rows; any failures are reported back rather than aborting the purge.

    Args:
        db: The active SQLAlchemy session.
        ds: The dataset to remove.

    Returns:
        A summary: objects deleted, bytes freed, and any per-object storage errors.
    """
    dataset_id = ds.dataset_id
    assets = db.query(DataAsset).filter(DataAsset.dataset_id == dataset_id).all()
    objects_deleted, bytes_freed, storage_errors = 0, 0, []
    for asset in assets:
        try:
            storage.delete_object(asset.uri)
            objects_deleted += 1
            bytes_freed += _count(asset.size_bytes)
        except Exception as exc:  # noqa: BLE001 — report, don't abort the DB purge
            storage_errors.append(f"{_filename(asset.uri) or asset.uri}: {exc}")

    # Annotations first: representations reference assets, annotations reference sets and
    # assets, so they must go before data_assets. Purging a dataset removes its published
    # annotations too — unlike a re-ingest, this is an explicit "delete everything" action.
    db.execute(text("""
        DELETE FROM annotation_representations WHERE annotation_id IN (
            SELECT a.annotation_id FROM annotations a
            JOIN annotation_sets s ON s.annotation_set_id = a.annotation_set_id
            WHERE s.dataset_id = :d)
    """), {"d": dataset_id})
    db.execute(text("""
        DELETE FROM annotations WHERE annotation_set_id IN (
            SELECT annotation_set_id FROM annotation_sets WHERE dataset_id = :d)
    """), {"d": dataset_id})
    db.execute(text("DELETE FROM annotation_sets WHERE dataset_id = :d"), {"d": dataset_id})
    # Derivatives point at their source asset; clear the self-reference before deleting.
    db.execute(text("UPDATE data_assets SET derived_from_asset_id = NULL WHERE dataset_id = :d"),
               {"d": dataset_id})
    db.execute(text("DELETE FROM data_assets WHERE dataset_id = :d"), {"d": dataset_id})
    case_subq = "SELECT case_id FROM cases WHERE dataset_id = :d"
    for table in _CASE_SCOPED_TABLES:
        db.execute(text(f"DELETE FROM {table} WHERE case_id IN ({case_subq})"), {"d": dataset_id})
    db.execute(text("DELETE FROM ingestion_runs WHERE dataset_id = :d"), {"d": dataset_id})
    db.execute(text("DELETE FROM cases WHERE dataset_id = :d"), {"d": dataset_id})
    db.query(Dataset).filter(Dataset.dataset_id == dataset_id).delete()
    db.commit()

    return {
        "deleted": dataset_id,
        "dataset_name": ds.name,
        "objects_deleted": objects_deleted,
        "bytes_freed": bytes_freed,
        "storage_errors": storage_errors,
    }


def _dataset_stats(db: Session) -> dict[int, dict]:
    """Read the dataset_stats view into a dataset_id-keyed mapping."""
    rows = db.execute(
        text("SELECT dataset_id, n_cases, n_files FROM dataset_stats")
    ).mappings().all()
    return {
        int(r["dataset_id"]): {
            "n_cases": _count(r["n_cases"]),
            "n_files": _count(r["n_files"]),
        }
        for r in rows
    }


def _latest_run(db: Session, dataset_id: Optional[int] = None) -> Optional[dict]:
    """Return the most recent ingestion run, optionally scoped to a dataset."""
    q = (
        db.query(IngestionRun, Dataset)
        .join(Dataset, IngestionRun.dataset_id == Dataset.dataset_id)
        .order_by(IngestionRun.started_at.desc().nullslast(), IngestionRun.run_id.desc())
    )
    if dataset_id is not None:
        q = q.filter(IngestionRun.dataset_id == dataset_id)
    row = q.first()
    if row is None:
        return None
    run, ds = row
    return {
        "run_id": run.run_id,
        "dataset_id": run.dataset_id,
        "dataset_name": ds.name,
        "connector": _value(run.connector),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": _value(run.status),
        "log_uri": run.log_uri,
    }


def _dataset_payload(ds: Dataset, stats: Optional[dict] = None) -> dict:
    """Serialize a dataset row with optional derived stats."""
    stats = stats or {"n_cases": 0, "n_files": 0}
    return {
        "dataset_id": ds.dataset_id,
        "name": ds.name,
        "access_type": _value(ds.access_type),
        "gi_cancer_types": ds.gi_cancer_types,
        "official_page": ds.official_page,
        "n_cases": stats["n_cases"],
        "n_files": stats["n_files"],
    }


def _case_count(db: Session, dataset_id: int) -> int:
    return _count(db.query(func.count(Case.case_id)).filter(Case.dataset_id == dataset_id).scalar())


def _count_joined_to_cases(db: Session, model, pk, dataset_id: int) -> int:
    """Count rows for a child table with a direct case_id column."""
    return _count(
        db.query(func.count(pk))
        .select_from(model)
        .join(Case, model.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id)
        .scalar()
    )


def _distribution(db: Session, label, query, limit: int = 8) -> list[dict]:
    """Group a query by one column and return label/count pairs."""
    rows = (
        query.group_by(label)
        .order_by(func.count().desc(), label.asc().nullslast())
        .limit(limit)
        .all()
    )
    return [
        {"label": str(_value(item) or "Unknown"), "count": _count(count)}
        for item, count in rows
    ]


def _missingness_metric(label, category, unit, total, present) -> dict:
    """Build one missingness/completeness response row."""
    total = _count(total)
    present = _count(present)
    return {
        "label": label,
        "category": category,
        "unit": unit,
        "total": total,
        "present": present,
        "missing": max(total - present, 0),
        "completeness_pct": _pct(present, total),
    }


def _asset_payload(asset: DataAsset, slide: Optional[Slide] = None,
                   sample: Optional[Sample] = None, case: Optional[Case] = None) -> dict:
    """Serialize a data asset with optional linkage context."""
    return {
        "asset_id": asset.asset_id,
        "dataset_id": asset.dataset_id,
        "asset_type": _value(asset.asset_type),
        "layer": _value(asset.layer),
        "uri": asset.uri,
        "file_name": _filename(asset.uri),
        "format": asset.format,
        "md5": asset.md5,
        "size_bytes": _count(asset.size_bytes),
        "source_file_id": asset.source_file_id,
        "created_at": asset.created_at,
        "slide_id": slide.slide_id if slide else asset.slide_id,
        "slide_barcode": slide.submitter_id if slide else None,
        "slide_type": slide.slide_type if slide else None,
        "sample_id": sample.sample_id if sample else None,
        "sample_barcode": sample.submitter_id if sample else None,
        "case_id": case.case_id if case else None,
        "case_barcode": case.submitter_id if case else None,
    }


@app.get("/health")
def health():
    """Liveness check."""
    return {"status": "ok"}


@app.get("/datasets")
def list_datasets(db: Session = Depends(get_db)):
    """List ingested datasets with derived counts from dataset_stats."""
    stats = _dataset_stats(db)
    datasets = db.query(Dataset).order_by(Dataset.dataset_id).all()
    return [_dataset_payload(d, stats.get(d.dataset_id)) for d in datasets]


@app.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: int, db: Session = Depends(get_db)):
    """Return one dataset plus latest run and derived counts."""
    ds = _dataset_or_404(db, dataset_id)
    stats = _dataset_stats(db).get(dataset_id)
    payload = _dataset_payload(ds, stats)
    payload["latest_run"] = _latest_run(db, dataset_id)
    return payload


@app.delete("/datasets/{dataset_id}")
def delete_dataset(dataset_id: int, db: Session = Depends(get_db)):
    """Permanently remove an ingested dataset: its stored slide files AND all of its
    Postgres records. Frees local disk. The download-registry entry is left intact so the
    dataset can be re-downloaded later; use DELETE /catalog/{id} to drop that separately."""
    ds = _dataset_or_404(db, dataset_id)
    return _purge_dataset(db, ds)


@app.get("/stats/overview")
def stats_overview(db: Session = Depends(get_db)):
    """High-level dashboard counts across all ingested data."""
    stats = _dataset_stats(db)
    datasets = db.query(Dataset).order_by(Dataset.dataset_id).all()
    total_asset_bytes = _count(db.query(func.sum(DataAsset.size_bytes)).scalar())

    access_rows = (
        db.query(Dataset.access_type, func.count(Dataset.dataset_id))
        .group_by(Dataset.access_type)
        .order_by(func.count(Dataset.dataset_id).desc())
        .all()
    )

    return {
        "datasets": _count(db.query(func.count(Dataset.dataset_id)).scalar()),
        "cases": _count(db.query(func.count(Case.case_id)).scalar()),
        "samples": _count(db.query(func.count(Sample.sample_id)).scalar()),
        "slides": _count(db.query(func.count(Slide.slide_id)).scalar()),
        "assets": _count(db.query(func.count(DataAsset.asset_id)).scalar()),
        "wsi_assets": _count(db.query(func.count(DataAsset.asset_id)).filter(DataAsset.asset_type == "wsi").scalar()),
        "annotations": _count(db.query(func.count(Annotation.annotation_id)).scalar()),
        "total_asset_bytes": total_asset_bytes,
        "latest_run": _latest_run(db),
        "by_access_type": [
            {"label": str(_value(access_type) or "Unknown"), "count": _count(count)}
            for access_type, count in access_rows
        ],
        "datasets_table": [_dataset_payload(d, stats.get(d.dataset_id)) for d in datasets],
    }


@app.get("/stats/dataset-stats")
def stats_dataset_stats(db: Session = Depends(get_db)):
    """Expose the dataset_stats database view directly for dashboard tables."""
    rows = db.execute(
        text(
            """
            SELECT ds.dataset_id, d.name, d.access_type, d.gi_cancer_types,
                   ds.n_cases, ds.n_files
            FROM dataset_stats ds
            JOIN datasets d ON d.dataset_id = ds.dataset_id
            ORDER BY ds.dataset_id
            """
        )
    ).mappings().all()
    return [
        {
            "dataset_id": r["dataset_id"],
            "name": r["name"],
            "access_type": r["access_type"],
            "gi_cancer_types": r["gi_cancer_types"],
            "n_cases": _count(r["n_cases"]),
            "n_files": _count(r["n_files"]),
        }
        for r in rows
    ]


# GDC project composition changes rarely, and the byte-volume scan pages tens of thousands of
# files, so cache the breakdown per project. Keyed by project id -> (fetched_at_epoch, payload).
_ACCESS_CACHE: dict = {}
_ACCESS_TTL = 6 * 3600  # 6 hours


@app.get("/datasets/{dataset_id}/access")
def dataset_access(dataset_id: int, db: Session = Depends(get_db)):
    """Live GDC open-vs-controlled file breakdown for this dataset's project (counts + bytes +
    per-category split). Cached ~6h. Non-GDC datasets return {available: false}."""
    ds = _dataset_or_404(db, dataset_id)
    import gdc_acquire
    try:
        project = gdc_acquire.resolve_project(ds.official_page or "")
    except gdc_acquire.AcquireError:
        return {"available": False, "reason": "Access breakdown is only available for GDC/TCGA datasets."}

    now = time.time()
    cached = _ACCESS_CACHE.get(project)
    if cached and now - cached[0] < _ACCESS_TTL:
        return cached[1]
    try:
        payload = gdc_acquire.access_breakdown(project)
    except Exception as exc:  # noqa: BLE001 — surface GDC failure as a 502
        raise HTTPException(status_code=502, detail=f"GDC query failed: {exc}") from exc
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    _ACCESS_CACHE[project] = (now, payload)
    return payload


@app.get("/datasets/{dataset_id}/summary")
def dataset_summary(dataset_id: int, db: Session = Depends(get_db)):
    """Dataset detail summary for the overview/detail dashboard."""
    ds = _dataset_or_404(db, dataset_id)
    case_count = _case_count(db, dataset_id)

    table_counts = {
        "cases": case_count,
        "diagnoses": _count_joined_to_cases(db, Diagnosis, Diagnosis.diagnosis_id, dataset_id),
        "treatments": _count_joined_to_cases(db, Treatment, Treatment.treatment_id, dataset_id),
        "pathology_details": _count_joined_to_cases(db, PathologyDetail, PathologyDetail.pathology_detail_id, dataset_id),
        "follow_ups": _count_joined_to_cases(db, FollowUp, FollowUp.follow_up_id, dataset_id),
        "molecular_tests": _count_joined_to_cases(db, MolecularTest, MolecularTest.molecular_test_id, dataset_id),
        "samples": _count_joined_to_cases(db, Sample, Sample.sample_id, dataset_id),
        "portions": _count_joined_to_cases(db, Portion, Portion.portion_id, dataset_id),
        "analytes": _count_joined_to_cases(db, Analyte, Analyte.analyte_id, dataset_id),
        "aliquots": _count_joined_to_cases(db, Aliquot, Aliquot.aliquot_id, dataset_id),
        "slides": _count_joined_to_cases(db, Slide, Slide.slide_id, dataset_id),
        "annotations": _count_joined_to_cases(db, Annotation, Annotation.annotation_id, dataset_id),
        "data_assets": _count(db.query(func.count(DataAsset.asset_id)).filter(DataAsset.dataset_id == dataset_id).scalar()),
    }

    distributions = {
        "sex_at_birth": _distribution(
            db,
            Case.sex_at_birth,
            db.query(Case.sex_at_birth, func.count(Case.case_id)).filter(Case.dataset_id == dataset_id),
        ),
        "vital_status": _distribution(
            db,
            Case.vital_status,
            db.query(Case.vital_status, func.count(Case.case_id)).filter(Case.dataset_id == dataset_id),
        ),
        "ajcc_stage": _distribution(
            db,
            Diagnosis.ajcc_pathologic_stage,
            db.query(Diagnosis.ajcc_pathologic_stage, func.count(Diagnosis.diagnosis_id))
            .join(Case, Diagnosis.case_id == Case.case_id)
            .filter(Case.dataset_id == dataset_id, Diagnosis.diagnosis_is_primary_disease.is_(True)),
        ),
        "sample_type": _distribution(
            db,
            Sample.sample_type,
            db.query(Sample.sample_type, func.count(Sample.sample_id))
            .join(Case, Sample.case_id == Case.case_id)
            .filter(Case.dataset_id == dataset_id),
        ),
        "slide_type": _distribution(
            db,
            Slide.slide_type,
            db.query(Slide.slide_type, func.count(Slide.slide_id))
            .join(Case, Slide.case_id == Case.case_id)
            .filter(Case.dataset_id == dataset_id),
        ),
    }

    asset_bytes = _count(
        db.query(func.sum(DataAsset.size_bytes))
        .filter(DataAsset.dataset_id == dataset_id)
        .scalar()
    )

    return {
        "dataset": _dataset_payload(ds, _dataset_stats(db).get(dataset_id)),
        "table_counts": table_counts,
        "distributions": distributions,
        "asset_bytes": asset_bytes,
        "latest_run": _latest_run(db, dataset_id),
    }


@app.get("/datasets/{dataset_id}/survival")
def dataset_survival(dataset_id: int, limit: int = Query(200, ge=1, le=1000),
                     db: Session = Depends(get_db)):
    """Return OS summary, histogram, and patient-level rows from case_survival."""
    _dataset_or_404(db, dataset_id)
    rows = db.execute(
        text(
            """
            SELECT cs.case_id, cs.submitter_id, cs.vital_status, cs.os_time, cs.os_event
            FROM case_survival cs
            JOIN cases c ON c.case_id = cs.case_id
            WHERE c.dataset_id = :dataset_id
            ORDER BY cs.submitter_id
            """
        ),
        {"dataset_id": dataset_id},
    ).mappings().all()

    os_times = [_count(r["os_time"]) for r in rows if r["os_time"] is not None]
    total = len(rows)
    with_os = len(os_times)
    dead = sum(1 for r in rows if _count(r["os_event"]) == 1)
    alive_or_censored = total - dead

    # Histogram + median use patients who DIED only. For those still alive, os_time is a
    # censoring time (how long we observed them), not their true survival — mixing it in
    # would understate survival. (A Kaplan-Meier estimate would use the censored patients
    # properly; that's a future enhancement.)
    death_times = sorted(
        _count(r["os_time"])
        for r in rows
        if _count(r["os_event"]) == 1 and r["os_time"] is not None
    )
    histogram = []
    if death_times:
        max_time = max(death_times)
        step = max(ceil(max_time / 10), 1)
        bins = [{"start": i * step, "end": ((i + 1) * step) - 1, "count": 0} for i in range(10)]
        for value in death_times:
            idx = min(value // step, 9)
            bins[int(idx)]["count"] += 1
        histogram = bins

    return {
        "summary": {
            "total_cases": total,
            "with_os_time": with_os,
            "missing_os_time": total - with_os,
            "os_events_dead": dead,
            "alive_or_censored": alive_or_censored,
            "median_time_to_death": death_times[len(death_times) // 2] if death_times else None,
        },
        "histogram": histogram,
        "records": [
            {
                "case_id": r["case_id"],
                "case_barcode": r["submitter_id"],
                "vital_status": r["vital_status"],
                "os_time": r["os_time"],
                "os_event": r["os_event"],
            }
            for r in rows[:limit]
        ],
    }


@app.get("/datasets/{dataset_id}/missingness")
def dataset_missingness(dataset_id: int, db: Session = Depends(get_db)):
    """Completeness summary for the main clinical/linkage fields."""
    _dataset_or_404(db, dataset_id)
    case_total = _case_count(db, dataset_id)
    primary_dx = (
        db.query(Diagnosis)
        .join(Case, Diagnosis.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id, Diagnosis.diagnosis_is_primary_disease.is_(True))
    )
    primary_dx_total = _count(primary_dx.with_entities(func.count(Diagnosis.diagnosis_id)).scalar())

    sample_q = db.query(Sample).join(Case, Sample.case_id == Case.case_id).filter(Case.dataset_id == dataset_id)
    sample_total = _count(sample_q.with_entities(func.count(Sample.sample_id)).scalar())
    slide_q = db.query(Slide).join(Case, Slide.case_id == Case.case_id).filter(Case.dataset_id == dataset_id)
    slide_total = _count(slide_q.with_entities(func.count(Slide.slide_id)).scalar())
    treatment_q = db.query(Treatment).join(Case, Treatment.case_id == Case.case_id).filter(Case.dataset_id == dataset_id)
    treatment_total = _count(treatment_q.with_entities(func.count(Treatment.treatment_id)).scalar())
    follow_q = db.query(FollowUp).join(Case, FollowUp.case_id == Case.case_id).filter(Case.dataset_id == dataset_id)
    follow_total = _count(follow_q.with_entities(func.count(FollowUp.follow_up_id)).scalar())
    molecular_q = db.query(MolecularTest).join(Case, MolecularTest.case_id == Case.case_id).filter(Case.dataset_id == dataset_id)
    molecular_total = _count(molecular_q.with_entities(func.count(MolecularTest.molecular_test_id)).scalar())

    metrics = [
        _missingness_metric(
            "Sex at birth", "case demographics", "cases", case_total,
            db.query(func.count(Case.sex_at_birth)).filter(Case.dataset_id == dataset_id).scalar(),
        ),
        _missingness_metric(
            "Race", "case demographics", "cases", case_total,
            db.query(func.count(Case.race)).filter(Case.dataset_id == dataset_id).scalar(),
        ),
        _missingness_metric(
            "Vital status", "outcome", "cases", case_total,
            db.query(func.count(Case.vital_status)).filter(Case.dataset_id == dataset_id).scalar(),
        ),
        _missingness_metric(
            "Days to death", "outcome", "cases", case_total,
            db.query(func.count(Case.days_to_death)).filter(Case.dataset_id == dataset_id).scalar(),
        ),
        _missingness_metric(
            "Age at diagnosis", "diagnosis", "primary diagnoses", primary_dx_total,
            primary_dx.with_entities(func.count(Diagnosis.age_at_diagnosis)).scalar(),
        ),
        _missingness_metric(
            "AJCC stage", "diagnosis", "primary diagnoses", primary_dx_total,
            primary_dx.with_entities(func.count(Diagnosis.ajcc_pathologic_stage)).scalar(),
        ),
        _missingness_metric(
            "AJCC T", "diagnosis", "primary diagnoses", primary_dx_total,
            primary_dx.with_entities(func.count(Diagnosis.ajcc_pathologic_t)).scalar(),
        ),
        _missingness_metric(
            "AJCC N", "diagnosis", "primary diagnoses", primary_dx_total,
            primary_dx.with_entities(func.count(Diagnosis.ajcc_pathologic_n)).scalar(),
        ),
        _missingness_metric(
            "AJCC M", "diagnosis", "primary diagnoses", primary_dx_total,
            primary_dx.with_entities(func.count(Diagnosis.ajcc_pathologic_m)).scalar(),
        ),
        _missingness_metric(
            "Treatment type", "treatment", "treatments", treatment_total,
            treatment_q.with_entities(func.count(Treatment.treatment_type)).scalar(),
        ),
        _missingness_metric(
            "Follow-up days", "follow-up", "follow-ups", follow_total,
            follow_q.with_entities(func.count(FollowUp.days_to_follow_up)).scalar(),
        ),
        _missingness_metric(
            "Molecular lab test", "molecular", "molecular tests", molecular_total,
            molecular_q.with_entities(func.count(MolecularTest.laboratory_test)).scalar(),
        ),
        _missingness_metric(
            "Sample type", "biospecimen", "samples", sample_total,
            sample_q.with_entities(func.count(Sample.sample_type)).scalar(),
        ),
        _missingness_metric(
            "Slide type", "slides", "slides", slide_total,
            slide_q.with_entities(func.count(Slide.slide_type)).scalar(),
        ),
        _missingness_metric(
            "Percent tumor cells", "slides", "slides", slide_total,
            slide_q.with_entities(func.count(Slide.percent_tumor_cells)).scalar(),
        ),
    ]
    return {"dataset_id": dataset_id, "metrics": metrics}


@app.get("/datasets/{dataset_id}/linkage")
def dataset_linkage(dataset_id: int, db: Session = Depends(get_db)):
    """Patient -> sample -> slide -> WSI linkage counts."""
    _dataset_or_404(db, dataset_id)

    total_cases = _case_count(db, dataset_id)
    total_samples = _count_joined_to_cases(db, Sample, Sample.sample_id, dataset_id)
    total_slides = _count_joined_to_cases(db, Slide, Slide.slide_id, dataset_id)
    total_wsi_assets = _count(
        db.query(func.count(DataAsset.asset_id))
        .filter(DataAsset.dataset_id == dataset_id, DataAsset.asset_type == "wsi")
        .scalar()
    )

    cases_with_samples = _count(
        db.query(func.count(distinct(Sample.case_id)))
        .join(Case, Sample.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id)
        .scalar()
    )
    cases_with_slides = _count(
        db.query(func.count(distinct(Slide.case_id)))
        .join(Case, Slide.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id)
        .scalar()
    )
    slides_with_assets = _count(
        db.query(func.count(distinct(Slide.slide_id)))
        .join(DataAsset, DataAsset.slide_id == Slide.slide_id)
        .join(Case, Slide.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id, DataAsset.asset_type == "wsi")
        .scalar()
    )
    cases_with_wsi_assets = _count(
        db.query(func.count(distinct(Slide.case_id)))
        .join(DataAsset, DataAsset.slide_id == Slide.slide_id)
        .join(Case, Slide.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id, DataAsset.asset_type == "wsi")
        .scalar()
    )
    cases_with_survival = _count(
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT cs.case_id)
                FROM case_survival cs
                JOIN cases c ON c.case_id = cs.case_id
                WHERE c.dataset_id = :dataset_id AND cs.os_time IS NOT NULL
                """
            ),
            {"dataset_id": dataset_id},
        ).scalar()
    )
    cases_with_slides_no_survival = _count(
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT s.case_id)
                FROM slides s
                JOIN cases c ON c.case_id = s.case_id
                LEFT JOIN case_survival cs ON cs.case_id = c.case_id
                WHERE c.dataset_id = :dataset_id AND cs.os_time IS NULL
                """
            ),
            {"dataset_id": dataset_id},
        ).scalar()
    )
    cases_with_survival_no_wsi = _count(
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT c.case_id)
                FROM cases c
                JOIN case_survival cs ON cs.case_id = c.case_id
                WHERE c.dataset_id = :dataset_id
                  AND cs.os_time IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM slides s
                      JOIN data_assets a ON a.slide_id = s.slide_id
                      WHERE s.case_id = c.case_id AND a.asset_type = 'wsi'
                  )
                """
            ),
            {"dataset_id": dataset_id},
        ).scalar()
    )

    return {
        "dataset_id": dataset_id,
        "chain": [
            {"label": "Cases", "count": total_cases},
            {"label": "Cases with samples", "count": cases_with_samples},
            {"label": "Samples", "count": total_samples},
            {"label": "Cases with slide metadata", "count": cases_with_slides},
            {"label": "Slide metadata rows", "count": total_slides},
            {"label": "Slides with downloaded WSI assets", "count": slides_with_assets},
            {"label": "Downloaded WSI assets", "count": total_wsi_assets},
        ],
        "checks": {
            "cases_without_samples": max(total_cases - cases_with_samples, 0),
            "slides_without_wsi_asset": max(total_slides - slides_with_assets, 0),
            "cases_with_wsi_assets": cases_with_wsi_assets,
            "cases_with_survival": cases_with_survival,
            "cases_with_slides_no_survival": cases_with_slides_no_survival,
            "cases_with_survival_no_wsi": cases_with_survival_no_wsi,
        },
    }


@app.get("/datasets/{dataset_id}/slides")
def dataset_slides(dataset_id: int,
                   downloaded_only: bool = Query(False),
                   limit: int = Query(200, ge=1, le=1000),
                   offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db)):
    """List slide metadata rows, optionally restricted to downloaded WSI assets."""
    _dataset_or_404(db, dataset_id)
    q = (
        db.query(Slide, Case, Sample, DataAsset)
        .join(Case, Slide.case_id == Case.case_id)
        .join(Sample, Slide.sample_id == Sample.sample_id)
        .outerjoin(DataAsset, DataAsset.slide_id == Slide.slide_id)
        .filter(Case.dataset_id == dataset_id)
    )
    if downloaded_only:
        q = q.filter(DataAsset.asset_id.isnot(None))
    # Count distinct slides — the asset outerjoin can fan a slide into several rows.
    total = _count(q.with_entities(func.count(distinct(Slide.slide_id))).scalar())
    rows = (
        q.order_by(Slide.slide_type.asc().nullslast(), Slide.submitter_id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "dataset_id": dataset_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "slides": [
            {
                "slide_id": slide.slide_id,
                "slide_barcode": slide.submitter_id,
                "slide_type": slide.slide_type,
                "section_location": _value(slide.section_location),
                "percent_tumor_cells": slide.percent_tumor_cells,
                "percent_tumor_nuclei": slide.percent_tumor_nuclei,
                "percent_necrosis": slide.percent_necrosis,
                "case_id": case.case_id,
                "case_barcode": case.submitter_id,
                "sample_id": sample.sample_id,
                "sample_barcode": sample.submitter_id,
                "sample_type": sample.sample_type,
                "tissue_type": _value(sample.tissue_type),
                "asset": _asset_payload(asset, slide, sample, case) if asset else None,
            }
            for slide, case, sample, asset in rows
        ],
    }


@app.get("/datasets/{dataset_id}/cases")
def dataset_cases(dataset_id: int, db: Session = Depends(get_db)):
    """Per-patient rows for the cohort explorer: key attributes + any viewable slides."""
    _dataset_or_404(db, dataset_id)

    # Primary-diagnosis stage per case.
    stage = {
        cid: s
        for cid, s in db.query(Diagnosis.case_id, Diagnosis.ajcc_pathologic_stage)
        .join(Case, Diagnosis.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id, Diagnosis.diagnosis_is_primary_disease.is_(True))
        .all()
    }

    # Survival (OS) per case, from the case_survival view. Keyed by str() because the
    # raw-SQL case_id and the ORM Case.case_id can be different Python types (UUID vs str).
    surv = {
        str(r["case_id"]): (r["os_time"], r["os_event"])
        for r in db.execute(
            text(
                """
                SELECT cs.case_id, cs.os_time, cs.os_event
                FROM case_survival cs
                JOIN cases c ON c.case_id = cs.case_id
                WHERE c.dataset_id = :dataset_id
                """
            ),
            {"dataset_id": dataset_id},
        ).mappings().all()
    }

    # Downloaded WSI slide images per case (what the viewer can actually open).
    slides_by_case: dict = {}
    slide_rows = (
        db.query(Slide.case_id, DataAsset.asset_id, Slide.submitter_id, Slide.slide_type)
        .join(DataAsset, DataAsset.slide_id == Slide.slide_id)
        .join(Case, Slide.case_id == Case.case_id)
        .filter(Case.dataset_id == dataset_id, DataAsset.asset_type == "wsi")
        .all()
    )
    for cid, aid, barcode, slide_type in slide_rows:
        slides_by_case.setdefault(cid, []).append(
            {"asset_id": aid, "slide_barcode": barcode, "slide_type": slide_type}
        )

    cases = db.query(Case).filter(Case.dataset_id == dataset_id).order_by(Case.submitter_id).all()
    return [
        {
            "case_id": c.case_id,
            "case_barcode": c.submitter_id,
            "sex": _value(c.sex_at_birth),
            "vital_status": _value(c.vital_status),
            "stage": stage.get(c.case_id),
            "os_time": surv.get(str(c.case_id), (None, None))[0],
            "os_event": surv.get(str(c.case_id), (None, None))[1],
            "slides": slides_by_case.get(c.case_id, []),
        }
        for c in cases
    ]


@app.get("/assets")
def list_assets(dataset_id: Optional[int] = Query(None),
                limit: int = Query(200, ge=1, le=1000),
                offset: int = Query(0, ge=0),
                db: Session = Depends(get_db)):
    """List object-storage assets with slide/sample/case linkage where available."""
    q = (
        db.query(DataAsset, Slide, Sample, Case)
        .outerjoin(Slide, DataAsset.slide_id == Slide.slide_id)
        .outerjoin(Sample, Slide.sample_id == Sample.sample_id)
        .outerjoin(Case, Slide.case_id == Case.case_id)
    )
    if dataset_id is not None:
        _dataset_or_404(db, dataset_id)
        q = q.filter(DataAsset.dataset_id == dataset_id)
    total = q.count()
    rows = q.order_by(DataAsset.asset_id).offset(offset).limit(limit).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "assets": [_asset_payload(asset, slide, sample, case) for asset, slide, sample, case in rows],
    }


@app.get("/assets/{asset_id}")
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    """Return one data asset with linkage context."""
    row = (
        db.query(DataAsset, Slide, Sample, Case)
        .outerjoin(Slide, DataAsset.slide_id == Slide.slide_id)
        .outerjoin(Sample, Slide.sample_id == Sample.sample_id)
        .outerjoin(Case, Slide.case_id == Case.case_id)
        .filter(DataAsset.asset_id == asset_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")
    asset, slide, sample, case = row
    return _asset_payload(asset, slide, sample, case)


@app.get("/assets/{asset_id}/download-url")
def get_asset_download_url(asset_id: int, expires: int = Query(3600, ge=60, le=86400),
                           db: Session = Depends(get_db)):
    """Return a temporary signed URL for an object-storage asset."""
    asset = db.query(DataAsset).filter(DataAsset.asset_id == asset_id).one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")
    try:
        url = storage.url_for(asset.uri, expires=expires)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not sign asset URL: {exc}") from exc
    return {"asset_id": asset.asset_id, "uri": asset.uri, "expires": expires, "url": url}


def _data_asset_or_404(db: Session, asset_id: int) -> DataAsset:
    """Fetch a DataAsset by id or raise a 404."""
    asset = db.query(DataAsset).filter(DataAsset.asset_id == asset_id).one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")
    return asset


@app.get("/slides/{asset_id}/info")
def slide_info(asset_id: int, db: Session = Depends(get_db)):
    """DeepZoom viewer metadata for a WSI asset (dimensions, tile geometry, MPP)."""
    asset = _data_asset_or_404(db, asset_id)
    try:
        return wsi.info(asset.asset_id, asset.uri)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not open slide: {exc}") from exc


@app.get("/slides/{asset_id}/tile/{level}/{col}/{row}")
def slide_tile(asset_id: int, level: int, col: int, row: int, db: Session = Depends(get_db)):
    """Serve one DeepZoom tile (JPEG) for the OpenSeadragon viewer."""
    asset = _data_asset_or_404(db, asset_id)
    try:
        data = wsi.tile(asset.asset_id, asset.uri, level, col, row)
    except IndexError:
        raise HTTPException(status_code=404, detail="Tile out of range")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Tile error: {exc}") from exc
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/slides/{asset_id}/thumbnail")
def slide_thumbnail(asset_id: int, db: Session = Depends(get_db)):
    """Serve a downscaled overview image (JPEG) of a WSI asset."""
    asset = _data_asset_or_404(db, asset_id)
    try:
        data = wsi.thumbnail(asset.asset_id, asset.uri)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Thumbnail error: {exc}") from exc
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------
# Annotations
#
# Every annotation payload carries its set's provenance (provider, method, licence,
# citation) and an explicit `is_spatial` flag, so a caller can never mistake a published
# algorithmic result for a project-generated or pathologist-reviewed label.
# ---------------------------------------------------------------------------
_SPATIAL_SCOPES = ("region", "nucleus", "patch", "tile")

# Source-defined GDC categories that matter when selecting a cohort. These are the
# source's own words; the grouping below is a UI convenience, not a source judgement.
_FLAG_GROUPS = {
    "dnu": ["Item flagged DNU"],
    "redaction": ["Subject withdrew consent", "Item is noncanonical.redaction"],
    "qc_failure": ["Center QC failed", "Item may not meet study protocol",
                   "Item does not meet study protocol", "Pathology outside specification",
                   "Qualification metrics changed", "Inadvertently shipped"],
    "noncanonical": ["Item is noncanonical"],
    "prior_malignancy": ["Prior malignancy", "Synchronous malignancy",
                         "History of acceptable prior treatment related to a prior/other malignancy",
                         "History of unacceptable prior treatment related to a prior/other malignancy"],
}


def _flag_group(category: Optional[str], classification: Optional[str]) -> Optional[str]:
    """Map a source category/classification onto a UI filter group, or None.

    This is presentation grouping only — it never re-labels the source values, which are
    always returned verbatim alongside it.
    """
    if classification == "Redaction":
        return "redaction"
    for group, cats in _FLAG_GROUPS.items():
        if category in cats:
            return group
    return None


def _set_payload(s: AnnotationSet) -> dict:
    """Serialize an annotation set — the provenance record for its annotations."""
    return {
        "annotation_set_id": s.annotation_set_id,
        "dataset_id": s.dataset_id,
        "name": s.name,
        "provider": s.provider,
        "source_url": s.source_url,
        "citation": s.citation,
        "license": s.license,
        "version": s.version,
        "method": s.method,
        "origin": s.origin,
        "description": s.description,
        "retrieved_at": s.retrieved_at,
        "is_algorithmic": s.method == "algorithmic",
        "is_published_derived": s.origin == "published_derived",
    }


def _annotation_payload(a: Annotation, s: AnnotationSet, n_reps: int = 0) -> dict:
    """Serialize an annotation together with its provenance and spatial status."""
    return {
        "annotation_id": a.annotation_id,
        "source_annotation_id": a.source_annotation_id,
        "case_id": a.case_id,
        "target_asset_id": a.target_asset_id,
        "scope": a.scope,
        "is_spatial": a.scope in _SPATIAL_SCOPES,
        "annotation_type": a.annotation_type,
        "label": a.label,
        "category": a.category,
        "classification": a.classification,
        "value_text": a.value_text,
        "value_number": a.value_number,
        "units": a.units,
        "confidence": a.confidence,
        "review_status": a.review_status,
        "source_entity_type": a.source_entity_type,
        "source_entity_submitter_id": a.source_entity_submitter_id,
        "notes": a.notes,
        "source_created_datetime": a.source_created_datetime,
        "flag_group": _flag_group(a.category, a.classification),
        "representation_count": n_reps,
        "annotation_set": _set_payload(s),
    }


def _annotations_query(db: Session):
    """Base query joining annotations to their set, with a representation count."""
    rep_count = (
        db.query(AnnotationRepresentation.annotation_id.label("aid"),
                 func.count(AnnotationRepresentation.representation_id).label("n"))
        .group_by(AnnotationRepresentation.annotation_id)
        .subquery()
    )
    return (
        db.query(Annotation, AnnotationSet, func.coalesce(rep_count.c.n, 0))
        .join(AnnotationSet, Annotation.annotation_set_id == AnnotationSet.annotation_set_id)
        .outerjoin(rep_count, rep_count.c.aid == Annotation.annotation_id)
    )


@app.get("/annotation-sets")
def list_annotation_sets(dataset_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    """List annotation collections and their provenance, with annotation counts."""
    q = db.query(AnnotationSet)
    if dataset_id is not None:
        q = q.filter(AnnotationSet.dataset_id == dataset_id)
    sets = q.order_by(AnnotationSet.annotation_set_id).all()
    counts = dict(
        db.query(Annotation.annotation_set_id, func.count(Annotation.annotation_id))
        .group_by(Annotation.annotation_set_id).all()
    )
    return {
        "total": len(sets),
        "annotation_sets": [
            {**_set_payload(s), "annotation_count": _count(counts.get(s.annotation_set_id, 0))}
            for s in sets
        ],
    }


@app.get("/cases/{case_id}/annotations")
def case_annotations(case_id: str, db: Session = Depends(get_db)):
    """All annotations recorded against a case (spatial and non-spatial)."""
    rows = (_annotations_query(db)
            .filter(Annotation.case_id == case_id)
            .order_by(Annotation.scope, Annotation.category, Annotation.annotation_id)
            .all())
    return {
        "case_id": case_id,
        "total": len(rows),
        "annotations": [_annotation_payload(a, s, n) for a, s, n in rows],
    }


@app.get("/assets/{asset_id}/annotations")
def asset_annotations(asset_id: int, db: Session = Depends(get_db)):
    """Annotations targeting one exact WSI asset."""
    _data_asset_or_404(db, asset_id)
    rows = (_annotations_query(db)
            .filter(Annotation.target_asset_id == asset_id)
            .order_by(Annotation.annotation_id)
            .all())
    return {
        "asset_id": asset_id,
        "total": len(rows),
        "annotations": [_annotation_payload(a, s, n) for a, s, n in rows],
    }


@app.get("/assets/{asset_id}/spatial-representations")
def asset_spatial_representations(asset_id: int, db: Session = Depends(get_db)):
    """Renderable spatial layers for a WSI, with the geometry needed to place them.

    `transform_metadata` carries the recorded scale/offset from the source, so the viewer
    positions a layer from measured numbers instead of stretching it to fit.
    """
    _data_asset_or_404(db, asset_id)
    rows = (
        db.query(AnnotationRepresentation, Annotation, AnnotationSet, DataAsset)
        .join(Annotation, AnnotationRepresentation.annotation_id == Annotation.annotation_id)
        .join(AnnotationSet, Annotation.annotation_set_id == AnnotationSet.annotation_set_id)
        .join(DataAsset, AnnotationRepresentation.asset_id == DataAsset.asset_id)
        .filter(Annotation.target_asset_id == asset_id)
        .order_by(Annotation.annotation_id, AnnotationRepresentation.representation_id)
        .all()
    )
    # A rendering derivative is typed `rendering_derivative` whatever it depicts, so on its own
    # it cannot say whether it draws a continuous probability map or a thresholded binary mask.
    # Carry the ORIGINAL's type across from its sibling under the same annotation: a viewer that
    # guesses would label a binary mask with a 0–1 probability scale.
    source_types = {
        ann.annotation_id: rep.representation_type
        for rep, ann, _aset, rep_asset in rows
        if rep_asset.asset_type == "annotation_source"
    }

    layers = []
    for rep, ann, aset, rep_asset in rows:
        # Only the browser-renderable derivative can be drawn; the preserved source file
        # is offered as a download instead.
        renderable = rep_asset.format in ("png", "jpeg", "jpg")
        layers.append({
            "source_representation_type": source_types.get(
                ann.annotation_id, rep.representation_type),
            "representation_id": rep.representation_id,
            "annotation_id": ann.annotation_id,
            "annotation_label": ann.label,
            "annotation_type": ann.annotation_type,
            "representation_type": rep.representation_type,
            "coordinate_space": rep.coordinate_space,
            "width": rep.width,
            "height": rep.height,
            "level": rep.level,
            "minimum_value": rep.minimum_value,
            "maximum_value": rep.maximum_value,
            "transform_metadata": rep.transform_metadata,
            "asset_id": rep_asset.asset_id,
            "asset_format": rep_asset.format,
            "asset_type": rep_asset.asset_type,
            "is_renderable": renderable,
            "is_source_original": rep_asset.asset_type == "annotation_source",
            "image_url": (f"/annotations/representations/{rep.representation_id}/image"
                          if renderable else None),
            "annotation_set": _set_payload(aset),
        })
    return {"asset_id": asset_id, "total": len(layers), "layers": layers}


@app.get("/annotations/representations/{representation_id}/image")
def representation_image(representation_id: int, db: Session = Depends(get_db)):
    """Serve a spatial representation's rendered image for the viewer overlay."""
    row = (
        db.query(AnnotationRepresentation, DataAsset)
        .join(DataAsset, AnnotationRepresentation.asset_id == DataAsset.asset_id)
        .filter(AnnotationRepresentation.representation_id == representation_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Representation {representation_id} not found")
    rep, asset = row
    if asset.format not in ("png", "jpeg", "jpg"):
        raise HTTPException(
            status_code=415,
            detail=(f"Representation {representation_id} is stored as {asset.format!r}, which the "
                    "browser cannot render directly. Use the asset download URL instead."),
        )
    try:
        data = wsi.fetch_bytes(asset.asset_id, asset.uri)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read overlay: {exc}") from exc
    media = "image/png" if asset.format == "png" else "image/jpeg"
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------
# Patient clinical timeline
# ---------------------------------------------------------------------------
# Ordering for events that share a day, so a day's group reads in clinical order.
_EVENT_ORDER = {
    "diagnosis": 0, "sample_collection": 1, "slide_available": 2, "treatment_start": 3,
    "treatment_end": 4, "molecular_test": 5, "follow_up": 6, "progression": 7,
    "recurrence": 8, "last_follow_up": 9, "death": 10, "other_diagnosis": 11,
    # Administrative rather than clinical, so it sorts below the patient's own events.
    "sample_received": 12,
}


def _collapse_identical(events):
    """Merge same-day events that are indistinguishable, keeping every source id.

    TCGA files successive follow-up form revisions (`_follow_up`, `_follow_up2`, …) that
    RESTATE the last known assessment instead of replacing it, so roughly half of all
    follow-up days carry more than one record saying exactly the same thing. Rendered
    verbatim that reads as a duplication bug.

    Only records identical in every displayed field are merged. Records that share a day but
    DISAGREE (~5% of multi-record days carry conflicting `disease_response`) stay separate —
    that disagreement is real signal and must remain visible. `source_count` reports how many
    source records a card stands for, and `ref_ids` keeps all of them addressable.

    Args:
        events: Event dicts for a single day.

    Returns:
        A new list with indistinguishable events merged.
    """
    merged = {}
    for e in events:
        key = (e["event_type"], e["label"], e["detail"], e["timing_basis"], e["asset_id"])
        if key in merged:
            merged[key]["source_count"] += 1
            merged[key]["ref_ids"].append(e["ref_id"])
        else:
            merged[key] = {**e, "ref_ids": [e["ref_id"]]}
    return list(merged.values())


def _drop_redundant_last_follow_up(timed, case):
    """Remove the last-known-follow-up card when it lands on the patient's death day.

    TCGA records a death by setting last contact to the death day: `days_to_last_follow_up`
    equals `days_to_death` for every dead patient in the cohort. The card therefore carries
    no information a dead patient's timeline doesn't already show, while crowding the column.
    It is kept for living patients, where last contact is the most recent real assessment.

    Args:
        timed: {day: [event, ...]}, mutated in place.
        case: The Case row, for `days_to_death`.
    """
    if case.days_to_death is None:
        return
    day = int(case.days_to_death)
    if day in timed:
        timed[day] = [e for e in timed[day] if e["event_type"] != "last_follow_up"]


@app.get("/cases/{case_id}/timeline")
def case_timeline(case_id: str, db: Session = Depends(get_db)):
    """Longitudinal clinical events for one patient, grouped by relative day.

    Read from the `case_timeline` view, so every event is a source record — nothing is
    interpolated and no visit is invented. Events whose source carries no usable date are
    returned separately as `untimed` rather than being placed on the axis at a guess.

    Days are relative to the initial pathologic diagnosis (day 0), which is the unit TCGA
    records; there are no absolute calendar dates in the source.
    """
    case = db.query(Case).filter(Case.case_id == case_id).one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    rows = db.execute(text("""
        SELECT event_type, day, timing_basis, label, detail, ref_table, ref_id, asset_id
        FROM case_timeline WHERE case_id = :c
    """), {"c": case_id}).fetchall()

    timed, untimed = {}, []
    for r in rows:
        event = {
            "event_type": r[0], "day": r[1], "timing_basis": r[2], "label": r[3],
            "detail": r[4], "ref_table": r[5], "ref_id": r[6], "asset_id": r[7],
            "source_count": 1,
        }
        if r[1] is None:
            untimed.append(event)
        else:
            timed.setdefault(int(r[1]), []).append(event)

    for day in list(timed):
        timed[day] = _collapse_identical(timed[day])
    _drop_redundant_last_follow_up(timed, case)

    for events in timed.values():
        events.sort(key=lambda e: (_EVENT_ORDER.get(e["event_type"], 99), e["label"] or ""))
    untimed.sort(key=lambda e: (_EVENT_ORDER.get(e["event_type"], 99), e["label"] or ""))

    groups = [{"day": d, "events": timed[d]} for d in sorted(timed)]
    days = sorted(timed)
    return {
        "case_id": case_id,
        "case_barcode": case.submitter_id,
        "baseline": "initial pathologic diagnosis (day 0)",
        "day_unit": "days relative to diagnosis",
        "total_events": len(rows),
        # Counts SOURCE records, not cards: a collapsed card stands for `source_count` of
        # them, so the header total stays faithful to the source even though fewer cards show.
        "timed_event_count": sum(e["source_count"] for g in groups for e in g["events"]),
        "untimed_event_count": len(untimed),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "has_longitudinal_data": len(days) > 1,
        "groups": groups,
        "untimed": untimed,
    }


@app.get("/cases/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db)):
    """One patient's core record, with their diagnosis, slides and annotation counts."""
    row = (
        db.query(Case, Dataset)
        .join(Dataset, Case.dataset_id == Dataset.dataset_id)
        .filter(Case.case_id == case_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    case, ds = row
    dx = (
        db.query(Diagnosis)
        .filter(Diagnosis.case_id == case_id)
        .order_by(Diagnosis.diagnosis_is_primary_disease.desc().nullslast())
        .first()
    )
    slides = (
        db.query(Slide, DataAsset)
        .join(DataAsset, DataAsset.slide_id == Slide.slide_id)
        .filter(Slide.case_id == case_id, DataAsset.asset_type == "wsi")
        .order_by(Slide.submitter_id)
        .all()
    )
    n_ann = _count(db.query(func.count(Annotation.annotation_id))
                   .filter(Annotation.case_id == case_id).scalar())
    return {
        "case_id": case.case_id,
        "case_barcode": case.submitter_id,
        "dataset_id": ds.dataset_id,
        "dataset_name": ds.name,
        "primary_site": case.primary_site,
        "disease_type": case.disease_type,
        "sex_at_birth": _value(case.sex_at_birth),
        "vital_status": _value(case.vital_status),
        "days_to_death": case.days_to_death,
        "primary_diagnosis": dx.primary_diagnosis if dx else None,
        "ajcc_pathologic_stage": dx.ajcc_pathologic_stage if dx else None,
        "age_at_diagnosis": dx.age_at_diagnosis if dx else None,
        "annotation_count": n_ann,
        "slides": [
            {"slide_id": s.slide_id, "slide_barcode": s.submitter_id,
             "slide_type": s.slide_type, "asset_id": a.asset_id}
            for s, a in slides
        ],
    }


@app.get("/ingestion-runs")
def list_ingestion_runs(dataset_id: Optional[int] = Query(None),
                        limit: int = Query(100, ge=1, le=500),
                        db: Session = Depends(get_db)):
    """List ETL ingestion history."""
    q = (
        db.query(IngestionRun, Dataset)
        .join(Dataset, IngestionRun.dataset_id == Dataset.dataset_id)
        .order_by(IngestionRun.started_at.desc().nullslast(), IngestionRun.run_id.desc())
    )
    if dataset_id is not None:
        _dataset_or_404(db, dataset_id)
        q = q.filter(IngestionRun.dataset_id == dataset_id)
    return [
        {
            "run_id": run.run_id,
            "dataset_id": run.dataset_id,
            "dataset_name": ds.name,
            "connector": _value(run.connector),
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "status": _value(run.status),
            "log_uri": run.log_uri,
        }
        for run, ds in q.limit(limit).all()
    ]


# ---------------------------------------------------------------------------
# "Add data" — download registry + acquisition jobs
# ---------------------------------------------------------------------------
class CatalogCreate(BaseModel):
    name: str
    source_url: str
    gi_cancer_types: Optional[str] = None
    notes: Optional[str] = None


class ManifestRequest(BaseModel):
    limit: Optional[int] = 6  # total slides to sample; 0/None = all (full)


class DownloadRequest(BaseModel):
    target: str = "local"
    limit: Optional[int] = 6  # total slides to sample; 0/None = all (full)


def _job_payload(job: DownloadJob) -> dict:
    return {
        "id": job.id,
        "catalog_id": job.catalog_id,
        "project": job.project,
        "dataset_name": job.dataset_name,
        "target": job.target,
        "status": job.status,
        "message": job.message,
        "n_slides": job.n_slides,
        "bytes_done": job.bytes_done,
        "bytes_total": job.bytes_total,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


@app.get("/catalog")
def list_catalog(db: Session = Depends(get_db)):
    """The download registry, with per-entry ingest state + latest job status."""
    ds_by_name = {name: did for did, name in db.query(Dataset.dataset_id, Dataset.name).all()}
    out = []
    for c in db.query(DownloadCatalog).order_by(DownloadCatalog.id).all():
        latest = (
            db.query(DownloadJob)
            .filter(DownloadJob.catalog_id == c.id)
            .order_by(DownloadJob.id.desc())
            .first()
        )
        out.append({
            "id": c.id,
            "name": c.name,
            "source_url": c.source_url,
            "source_type": c.source_type,
            "gi_cancer_types": c.gi_cancer_types,
            "notes": c.notes,
            "downloadable": c.source_type == "gdc",
            "ingested": c.name in ds_by_name,
            "dataset_id": ds_by_name.get(c.name),  # the ingested dataset to purge, if any
            "latest_job": _job_payload(latest) if latest else None,
        })
    return out


@app.post("/catalog")
def add_catalog(body: CatalogCreate, db: Session = Depends(get_db)):
    """Add a dataset to the registry; the source type is auto-detected from the URL."""
    entry = DownloadCatalog(
        name=body.name.strip(),
        source_url=body.source_url.strip(),
        source_type=detect_source_type(body.source_url),
        gi_cancer_types=body.gi_cancer_types,
        notes=body.notes,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "name": entry.name, "source_type": entry.source_type}


@app.delete("/catalog/{catalog_id}")
def delete_catalog(catalog_id: int, db: Session = Depends(get_db)):
    """Remove a registry entry (does not touch already-ingested data)."""
    entry = db.get(DownloadCatalog, catalog_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Catalog entry {catalog_id} not found")
    db.delete(entry)
    db.commit()
    return {"deleted": catalog_id}


@app.post("/catalog/{catalog_id}/manifest")
def catalog_manifest(catalog_id: int, body: ManifestRequest, db: Session = Depends(get_db)):
    """Live GDC 'plan': the slides that would be downloaded (sample N or all) + the case count."""
    entry = db.get(DownloadCatalog, catalog_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Catalog entry {catalog_id} not found")
    if entry.source_type != "gdc":
        raise HTTPException(status_code=400, detail="No connector for this source yet (only GDC/TCGA is supported).")

    import gdc_acquire

    try:
        project = gdc_acquire.resolve_project(entry.source_url)
    except gdc_acquire.AcquireError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    limit = body.limit or 0  # 0 = all
    tmp = tempfile.mkdtemp(prefix="plan-")
    try:
        selected, _ = gdc_acquire.plan(project, tmp, limit)
        n_cases = gdc_acquire.count_cases(project)
    except gdc_acquire.AcquireError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GDC query failed: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total_mb = round(sum(h["_size"] for h in selected) / 1e6, 1)
    slides = [{
        "slide_type": h["_type"],
        "size_mb": round(h["_size"] / 1e6, 1),
        "case": h["_case"],
        "barcode": h["_barcode"],
        "file_name": h["file_name"],
        "md5": h.get("md5sum"),
    } for h in selected[:200]]  # cap the listed rows; counts/size reflect the full selection
    return {"project": project, "n_cases": n_cases, "n_slides": len(selected),
            "total_mb": total_mb, "full": not limit, "slides": slides}


@app.post("/catalog/{catalog_id}/download")
def start_download(catalog_id: int, body: DownloadRequest, db: Session = Depends(get_db)):
    """Kick off an acquire+ingest job (background subprocess); returns the job to poll."""
    entry = db.get(DownloadCatalog, catalog_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Catalog entry {catalog_id} not found")
    if entry.source_type != "gdc":
        raise HTTPException(status_code=400, detail="No connector for this source yet (only GDC/TCGA is supported).")
    target = body.target or "local"
    if target not in ("local", "aws"):
        raise HTTPException(status_code=400, detail=f"Unknown target {target!r}")
    if not storage.is_target_configured(target):
        raise HTTPException(status_code=400, detail="AWS S3 is not configured yet.")

    import gdc_acquire

    try:
        project = gdc_acquire.resolve_project(entry.source_url)
    except gdc_acquire.AcquireError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = DownloadJob(
        catalog_id=entry.id, project=project, dataset_name=entry.name, target=target,
        status="pending", started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    cmd = [
        sys.executable, "/etl/jobs/acquire_ingest.py",
        "--job-id", str(job.id), "--project", project, "--name", entry.name,
        "--page-url", entry.source_url, "--target", target, "--limit", str(body.limit or 0),
    ]
    if entry.gi_cancer_types:
        cmd += ["--cancer-types", entry.gi_cancer_types]
    subprocess.Popen(cmd)  # detached; the orchestrator updates the job row
    return _job_payload(job)


@app.get("/download-jobs")
def list_download_jobs(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    """Recent acquisition jobs."""
    jobs = db.query(DownloadJob).order_by(DownloadJob.id.desc()).limit(limit).all()
    return [_job_payload(j) for j in jobs]


@app.get("/download-jobs/{job_id}")
def get_download_job(job_id: int, db: Session = Depends(get_db)):
    """Status of one acquisition job (for polling)."""
    job = db.get(DownloadJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _job_payload(job)


@app.get("/storage-targets")
def storage_targets():
    """Which download destinations are usable (aws gated until configured)."""
    return {"local": True, "aws": storage.is_target_configured("aws")}
