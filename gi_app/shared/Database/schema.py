"""Pydantic request/response schemas.

Minimal for the data-layer phase — a couple of representative response models so the
pattern (paired `X` response + `XCreate` request, `from_attributes=True`) is established.
The full CRUD set is filled in during the web-tool phase alongside the FastAPI endpoints.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DatasetBase(BaseModel):
    """Fields shared by the dataset request and response schemas."""

    name: str
    access_type: str
    official_page: Optional[str] = None
    gi_cancer_types: Optional[str] = None


class DatasetCreate(DatasetBase):
    """Request body for creating a dataset."""

    dataset_id: int


class Dataset(DatasetBase):
    """Dataset as returned by the API."""

    dataset_id: int

    class Config:
        from_attributes = True


class DataAsset(BaseModel):
    """Object-storage file pointer as returned by the API."""

    asset_id: int
    dataset_id: int
    slide_id: Optional[str] = None
    asset_type: Optional[str] = None
    layer: Optional[str] = None
    uri: str
    format: Optional[str] = None
    md5: Optional[str] = None
    size_bytes: Optional[int] = None
    source_file_id: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
