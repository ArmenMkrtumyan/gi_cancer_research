"""Shared ETL helpers: TSV reading, value/enum normalization, barcode utilities.

The normalizers exist because the schema stores closed vocabularies as Postgres enums
(so raw GDC strings like "Alive", "not reported", "Tumor" must be mapped to the exact
enum members) while evolving vocabularies stay as free-text varchar.
"""

import csv
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_ROOT = os.environ.get("DATA_ROOT", "/data")


# --------------------------------------------------------------------------- #
# TSV reading
# --------------------------------------------------------------------------- #
def read_dicts(path):
    """Read a TSV into a list of row dicts.

    Not for files with duplicate column names — use read_rows for those (see diagnoses).

    A missing file is treated as zero rows (returns []), not an error: the acquisition
    step writes a per-entity TSV only when that entity has at least one row, so an absent
    file legitimately means the project has none of that entity (e.g. TCGA-PAAD has no
    molecular tests, so no molecular_tests.tsv is written).

    Args:
        path: Path to the TSV file.

    Returns:
        A list of dicts, one per data row (column name -> value); [] if the file is absent.
    """
    if not os.path.exists(path):
        logger.warning("TSV not found, treating as empty: %s", path)
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def read_rows(path):
    """Read a TSV as raw rows for index-based access (handles duplicate column names).

    A missing (or empty) file is treated as zero rows — see read_dicts for why.

    Args:
        path: Path to the TSV file.

    Returns:
        A (header, rows) tuple: the header list and a list of row lists;
        ([], []) if the file is absent or empty.
    """
    if not os.path.exists(path):
        logger.warning("TSV not found, treating as empty: %s", path)
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, [])
        return header, list(reader)


# --------------------------------------------------------------------------- #
# Scalar normalization
# --------------------------------------------------------------------------- #
_BLANK = {"", "na", "n/a", "none", "null", "--"}


def s(v):
    """Clean a string value.

    Args:
        v: The raw value.

    Returns:
        The stripped string, or None if it was empty/blank-like.
    """
    if v is None:
        return None
    v = str(v).strip()
    return None if v.lower() in _BLANK else v


def to_int(v):
    """Parse an integer.

    Args:
        v: The raw value.

    Returns:
        The int, or None if blank/unparseable.
    """
    v = s(v)
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def to_float(v):
    """Parse a float.

    Args:
        v: The raw value.

    Returns:
        The float, or None if blank/unparseable.
    """
    v = s(v)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def to_bool(v):
    """Parse a boolean.

    Args:
        v: The raw value (e.g. "true"/"yes"/"1").

    Returns:
        True/False, or None if blank.
    """
    v = s(v)
    if v is None:
        return None
    return v.lower() in ("true", "yes", "1", "t")


def to_dt(v):
    """Parse an ISO datetime.

    Args:
        v: The raw value (ISO date or datetime string).

    Returns:
        A datetime, or None if blank/unparseable.
    """
    v = s(v)
    if v is None:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        try:
            return datetime.fromisoformat(v.split("T")[0])
        except ValueError:
            return None


def days_to_years(v):
    """Convert a GDC age in days to whole years.

    Args:
        v: The raw age (in days; values already in years are passed through).

    Returns:
        Age in years, or None if blank.
    """
    d = to_int(v)
    if d is None:
        return None
    return int(d / 365.25) if abs(d) > 130 else d


# --------------------------------------------------------------------------- #
# Enum normalization (raw GDC value -> exact enum member, else fallback/None)
# --------------------------------------------------------------------------- #
def _key(v):
    """Normalize a raw value to an enum-comparison key (lowercased, spaces -> _).

    Args:
        v: The raw value.

    Returns:
        The normalized key, or None if blank.
    """
    v = s(v)
    return v.lower().replace(" ", "_") if v else None


def yes_no(v):
    """Map a raw value to the `yes_no` enum.

    Args:
        v: The raw value.

    Returns:
        "yes", "no", "not_reported", or None if blank.
    """
    k = _key(v)
    if k is None:
        return None
    if k == "yes":
        return "yes"
    if k == "no":
        return "no"
    return "not_reported"


def sex(v):
    """Map a raw value to the `sex_at_birth` enum.

    Args:
        v: The raw value.

    Returns:
        "male", "female", "unknown", or None if blank.
    """
    k = _key(v)
    if k is None:
        return None
    if k in ("male", "female"):
        return k
    return "unknown"


def vital(v):
    """Map a raw value to the `vital_status` enum.

    Args:
        v: The raw value.

    Returns:
        "alive", "dead", "not_reported", or None if blank.
    """
    k = _key(v)
    if k is None:
        return None
    if k == "alive":
        return "alive"
    if k == "dead":
        return "dead"
    return "not_reported"


_RACE = {
    "white",
    "black_or_african_american",
    "asian",
    "american_indian_or_alaska_native",
    "native_hawaiian_or_other_pacific_islander",
    "not_reported",
}


def race(v):
    """Map a raw value to the `race` enum.

    Args:
        v: The raw value.

    Returns:
        A known race member, "not_reported" if unrecognized, or None if blank.
    """
    k = _key(v)
    if k is None:
        return None
    return k if k in _RACE else "not_reported"


def ethnicity(v):
    """Map a raw value to the `ethnicity` enum.

    Args:
        v: The raw value.

    Returns:
        "hispanic_or_latino", "not_hispanic_or_latino", "not_reported", or None if blank.
    """
    k = _key(v)
    if k is None:
        return None
    if k in ("hispanic_or_latino", "not_hispanic_or_latino"):
        return k
    return "not_reported"


def tissue_type(v):
    """Map a raw value to the `tissue_type` enum.

    Args:
        v: The raw value.

    Returns:
        "tumor", "normal", or None if blank/unrecognized.
    """
    k = _key(v)
    if k is None:
        return None
    if k in ("tumor", "normal"):
        return k
    return None


def section_location(v):
    """Map a raw value to the `section_location` enum.

    Args:
        v: The raw value.

    Returns:
        "top", "bottom", "not_reported", or None if blank.
    """
    k = _key(v)
    if k is None:
        return None
    if k in ("top", "bottom"):
        return k
    return "not_reported"


# --------------------------------------------------------------------------- #
# Barcode utilities
# --------------------------------------------------------------------------- #
def case_barcode(barcode):
    """Reduce any TCGA barcode to its case (patient) barcode.

    Args:
        barcode: A full barcode, e.g. "TCGA-AA-3562-01A".

    Returns:
        The first three segments, e.g. "TCGA-AA-3562", or None if blank.
    """
    b = s(barcode)
    if not b:
        return None
    return "-".join(b.split("-")[:3])


def slide_type_from_barcode(barcode):
    """Derive the slide-type letters from a slide barcode.

    Args:
        barcode: A slide barcode, e.g. "TCGA-AA-3562-01A-01-BS1".

    Returns:
        The trailing letters (DX/TS/BS/MS), e.g. "BS", or None if blank.
    """
    b = s(barcode)
    if not b:
        return None
    last = b.split("-")[-1]
    letters = "".join(ch for ch in last if ch.isalpha())
    return letters or None


def dedup_by_pk(rows, pk):
    """Drop duplicate rows by primary-key value, keeping the first seen.

    Args:
        rows: A list of row dicts.
        pk: The primary-key field name.

    Returns:
        The de-duplicated list (schema notes ~25 repeated slide_ids, etc.).
    """
    seen, out = set(), []
    for r in rows:
        key = r.get(pk)
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
