"""Pydantic request/response schemas.

Minimal for the data-layer phase — a couple of representative response models so the
pattern (paired `X` response + `XCreate` request, `from_attributes=True`) is established.
The full CRUD set is filled in during the web-tool phase alongside the FastAPI endpoints.
"""

from datetime import datetime
from typing import Any, Optional

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
    source_uri: Optional[str] = None
    derived_from_asset_id: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Annotations. Provenance travels with every annotation: an AnnotationSet is always
# returned alongside its rows so a caller can tell where a label came from, whether a
# human or an algorithm produced it, and under what licence it may be used.
# ---------------------------------------------------------------------------
class AnnotationSet(BaseModel):
    """An imported annotation collection/release and its provenance."""

    annotation_set_id: int
    dataset_id: int
    name: str
    provider: Optional[str] = None
    source_url: Optional[str] = None
    citation: Optional[str] = None
    license: Optional[str] = None
    version: Optional[str] = None
    method: Optional[str] = None      # manual | algorithmic | mixed | not_reported
    origin: str                       # source_provided | published_derived
    description: Optional[str] = None
    retrieved_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class Annotation(BaseModel):
    """One logical annotation, with its provenance attached."""

    annotation_id: int
    source_annotation_id: str
    case_id: Optional[str] = None
    target_asset_id: Optional[int] = None
    scope: str
    is_spatial: bool = False
    annotation_type: Optional[str] = None
    label: Optional[str] = None
    category: Optional[str] = None
    classification: Optional[str] = None
    value_text: Optional[str] = None
    value_number: Optional[float] = None
    units: Optional[str] = None
    confidence: Optional[float] = None
    review_status: Optional[str] = None
    source_entity_type: Optional[str] = None
    source_entity_submitter_id: Optional[str] = None
    notes: Optional[str] = None
    source_created_datetime: Optional[datetime] = None
    flag_group: Optional[str] = None
    representation_count: int = 0
    annotation_set: Optional[AnnotationSet] = None

    class Config:
        from_attributes = True


class AnnotationRepresentation(BaseModel):
    """A file-backed representation of a spatial annotation."""

    representation_id: int
    annotation_id: int
    asset_id: int
    representation_type: Optional[str] = None
    coordinate_space: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    level: Optional[int] = None
    # Scale/offset placing the representation on slide level 0 (see til_overlay_import).
    transform_metadata: Optional[dict[str, Any]] = None
    minimum_value: Optional[float] = None
    maximum_value: Optional[float] = None

    class Config:
        from_attributes = True


class TimelineEvent(BaseModel):
    """One source-derived clinical event on a patient timeline."""

    event_type: str
    day: Optional[int] = None          # relative to diagnosis; None = source has no date
    timing_basis: str                  # baseline | relative_to_diagnosis | derived_* | unknown
    label: Optional[str] = None
    detail: Optional[str] = None
    ref_table: Optional[str] = None
    ref_id: Optional[str] = None
    asset_id: Optional[int] = None     # set for slide events, links to the WSI viewer

    class Config:
        from_attributes = True
