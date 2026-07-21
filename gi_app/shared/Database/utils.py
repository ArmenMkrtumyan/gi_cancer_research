"""Database helper functions (shared by the ETL loaders and, later, the API).

Kept next to the models since these operate directly on the database.
"""

from Database.models import Dataset


# Host/path fragment -> source type. Deliberately short: a source only earns an entry
# here once a connector can actually load it. Anything else is "other" and gets its
# compatibility report from analysis rather than a hardcoded guess (see shared/compat.py).
SOURCE_PATTERNS = (
    ("gdc.cancer.gov", "gdc"),
    ("ncbi.nlm.nih.gov/geo", "geo"),
    ("/geo/query/acc.cgi", "geo"),
)


def detect_source_type(url):
    """Classify a source URL so the download tool knows which connector (if any) fits.

    Args:
        url: The dataset's source link.

    Returns:
        "gdc", "geo", or "other".
    """
    u = (url or "").lower()
    for fragment, source_type in SOURCE_PATTERNS:
        if fragment in u:
            return source_type
    return "other"


def get_or_create_dataset(session, name, access_type, official_page=None, gi_cancer_types=None):
    """Insert or update a dataset row, keyed by name (idempotent).

    Each ingest loader calls this at the start of its run to register the dataset it's
    about to load, so the `datasets` catalog only ever holds actually-loaded datasets.

    Args:
        session: The active SQLAlchemy session.
        name: Canonical dataset name (the match key, e.g. "TCGA-COAD").
        access_type: Access-type enum value.
        official_page: Source URL (optional).
        gi_cancer_types: Cancer-type text (optional).

    Returns:
        The existing or newly created Dataset (with a surrogate dataset_id assigned).
    """
    ds = session.query(Dataset).filter_by(name=name).one_or_none()
    if ds is None:
        next_id = (session.query(Dataset).count() and
                   (max(d.dataset_id for d in session.query(Dataset).all()) + 1)) or 1
        ds = Dataset(dataset_id=next_id, name=name, access_type=access_type,
                     official_page=official_page, gi_cancer_types=gi_cancer_types)
        session.add(ds)
        session.flush()
    else:
        ds.access_type = access_type
        if official_page:
            ds.official_page = official_page
        if gi_cancer_types:
            ds.gi_cancer_types = gi_cancer_types
    return ds
