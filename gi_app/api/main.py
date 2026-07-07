"""FastAPI app — scaffold for the web-tool phase.

Only /health and a first read endpoint exist now; the Phase-1 dashboard endpoints
(/datasets, /stats/*, /datasets/{id}/linkage, slides, etc. per docs/ARCHITECTURE.md)
are built in the next phase against the shared DB layer.
"""

import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from Database.database import get_db
from Database.models import Dataset

app = FastAPI(title="GI Cancer Research API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Liveness check.

    Returns:
        {"status": "ok"} when the API is running.
    """
    return {"status": "ok"}


@app.get("/datasets")
def list_datasets(db: Session = Depends(get_db)):
    """List all datasets in the catalog.

    Args:
        db: Database session (injected).

    Returns:
        A list of dataset dicts (id, name, access_type, gi_cancer_types, official_page).
    """
    return [
        {"dataset_id": d.dataset_id, "name": d.name, "access_type": d.access_type,
         "gi_cancer_types": d.gi_cancer_types, "official_page": d.official_page}
        for d in db.query(Dataset).order_by(Dataset.dataset_id).all()
    ]
