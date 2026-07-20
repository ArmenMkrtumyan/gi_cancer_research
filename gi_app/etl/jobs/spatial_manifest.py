"""Build a reproducible manifest of published spatial annotations that match our slides.

Answers one question before anything is downloaded: *for each whole-slide image we
actually hold, which published annotation collections contain an annotation of that exact
slide?* Nothing here downloads a collection — it only resolves identifiers and writes a
manifest, so the expensive step stays deliberate.

Collections compared
--------------------
1. **TCGA-SBU-TIL-Maps** (IDC `tcga_sbu_til_maps`) — per-patch tumour-infiltrating-lymphocyte
   maps as DICOM Segmentation. Binary and fractional (probability) variants.
2. **Pan-Cancer-Nuclei-Seg** (IDC `pan_cancer_nuclei_seg_dicom`) — per-nucleus polygons.
3. **Kather TCGA-CRC tumour-only tile collections** (Zenodo) — extracted image tiles.

Matching
--------
IDC indexes slide microscopy by DICOM identifiers, not TCGA barcodes, so the match is made
on two independent keys that together identify one slide:

  * `PatientID` == the case part of our barcode (e.g. ``TCGA-CA-5255``), and
  * the slide-type token of our barcode (``DX1``/``TS1``/``BS1``…) appearing in the source
    `SeriesDescription`.

Where a segmentation names the series it was computed from, that reference is followed and
compared against the source slide's own record — this is what makes a match *exact* rather
than merely same-case. When the slide file is already available locally the level-0 pixel
dimensions are compared too, which is the strongest available check.

Usage (from the api container, which has OpenSlide + pyarrow):
    docker exec gi_app-api-1 python /etl/jobs/spatial_manifest.py --out /tmp/manifest.tsv
"""

import argparse
import csv
import logging
import os
import sys
import urllib.request

logger = logging.getLogger(__name__)

IDC_RELEASE = os.environ.get("IDC_INDEX_RELEASE", "24.2.2")
IDC_BASE = ("https://github.com/ImagingDataCommons/idc-index-data/releases/download/"
            f"{IDC_RELEASE}")
CACHE_DIR = os.environ.get("IDC_CACHE_DIR", "/tmp/idc-index-cache")

# Collections we resolve through the IDC index: analysis_result_id -> descriptor.
IDC_COLLECTIONS = {
    "tcga_sbu_til_maps": {
        "label": "TCGA-SBU-TIL-Maps",
        "source_url": "https://doi.org/10.5281/zenodo.16966285",
        "license": "CC BY 4.0",
        "representation": "probability_map / binary_mask (DICOM SEG)",
    },
    "pan_cancer_nuclei_seg_dicom": {
        "label": "Pan-Cancer-Nuclei-Seg-DICOM",
        "source_url": "https://doi.org/10.5281/zenodo.11099004",
        "license": "CC BY 4.0",
        "representation": "polygon / nuclei (DICOM SEG + ANN)",
    },
}

# Tile collections are handled separately: they are not slide-registered overlays.
TILE_COLLECTIONS = [{
    "label": "Kather TCGA-CRC tumour-only tiles (MSI/MSS)",
    "source_url": "https://doi.org/10.5281/zenodo.2530789",
    "license": "CC BY 4.0",
    "representation": "image tiles (PNG)",
    "note": ("VERIFIED not registerable: the archive's central directory was read directly "
             "(4001 filenames in TUMSTU.zip). Tiles are named with an opaque token "
             "(e.g. TUM-AAAEEYIDEMCV.png) — 0 of 4001 filenames contain a coordinate pair, "
             "and none carries a slide barcode. Without level-0 tile coordinates these "
             "cannot be reassembled into a registered slide overlay. Usable as a "
             "classification research asset only."),
}]

MANIFEST_COLUMNS = [
    "local_dataset", "local_asset_id", "slide_barcode", "source_collection",
    "source_identifier", "representation_type", "source_url", "source_size_bytes",
    "license", "exact_match_status", "download_status", "coordinate_notes",
]


def _fetch_index(name):
    """Download (and cache) one IDC index parquet, returning a DataFrame."""
    import pandas as pd

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{IDC_RELEASE}-{name}")
    if not os.path.exists(path):
        url = f"{IDC_BASE}/{name}"
        logger.info(f"Fetching IDC index {name} (release {IDC_RELEASE})...")
        urllib.request.urlretrieve(url, path)
    return pd.read_parquet(path)


def local_wsi_assets(session):
    """Return every registered WSI asset with its barcode and dataset.

    Args:
        session: The active SQLAlchemy session.

    Returns:
        A list of dicts with dataset name, asset_id, barcode and uri.
    """
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT d.name AS dataset, da.asset_id, s.submitter_id AS barcode, da.uri
        FROM data_assets da
        JOIN datasets d ON d.dataset_id = da.dataset_id
        LEFT JOIN slides s ON s.slide_id = da.slide_id
        WHERE da.asset_type = 'wsi'
        ORDER BY d.name, s.submitter_id
    """)).fetchall()
    return [dict(dataset=r[0], asset_id=r[1], barcode=r[2], uri=r[3]) for r in rows]


def split_barcode(barcode):
    """Split a TCGA slide barcode into its case part and slide-type token.

    Args:
        barcode: e.g. 'TCGA-CA-5255-01Z-00-DX1'.

    Returns:
        A (case_barcode, slide_token) tuple, e.g. ('TCGA-CA-5255', 'DX1'); the token is
        None when the barcode has no slide suffix.
    """
    if not barcode:
        return None, None
    parts = barcode.split("-")
    case = "-".join(parts[:3]) if len(parts) >= 3 else barcode
    token = parts[-1] if len(parts) >= 5 else None
    return case, token


def local_geometry(asset_id, uri, allow_download=False):
    """Level-0 dimensions of a local slide, if it can be opened without a big download.

    Args:
        asset_id: The asset's id (used to name the cache file).
        uri: The asset's s3:// URI.
        allow_download: Pull the slide from object storage when it is not already cached.

    Returns:
        A (width, height) tuple, or None when the slide is unavailable locally.
    """
    try:
        import openslide
        import storage
    except ImportError:
        return None
    cache = os.environ.get("SLIDE_CACHE_DIR", "/tmp/slide-cache")
    ext = os.path.splitext(uri)[1] or ".svs"
    path = os.path.join(cache, f"{asset_id}{ext}")
    if not os.path.exists(path):
        if not allow_download:
            return None
        os.makedirs(cache, exist_ok=True)
        storage.download_file(uri, path)
    try:
        osr = openslide.OpenSlide(path)
        dims = osr.dimensions
        osr.close()
        return dims
    except Exception as e:  # a corrupt/partial cache file must not abort the manifest
        logger.warning(f"could not read geometry for asset {asset_id}: {e}")
        return None


def build_rows(session, allow_download=False):
    """Resolve every local slide against the public collections.

    Args:
        session: The active SQLAlchemy session.
        allow_download: Whether geometry verification may pull slides from storage.

    Returns:
        A list of manifest row dicts.
    """
    idx = _fetch_index("idc_index.parquet")
    sm = _fetch_index("sm_index.parquet")
    seg = _fetch_index("seg_index.parquet")

    assets = local_wsi_assets(session)
    logger.info(f"{len(assets)} local WSI assets to resolve")
    rows = []

    for a in assets:
        case_bc, token = split_barcode(a["barcode"])
        if not case_bc:
            continue
        patient = idx[idx.PatientID == case_bc]
        # The source slide-microscopy series for this exact slide.
        src = patient[patient.Modality == "SM"]
        if token:
            src = src[src.SeriesDescription.astype(str).str.contains(token, na=False)]
        src = src.merge(sm, on="SeriesInstanceUID", how="left")

        local_dims = local_geometry(a["asset_id"], a["uri"], allow_download)
        src_uid, geom_note = None, ""
        if len(src):
            r0 = src.iloc[0]
            src_uid = r0.SeriesInstanceUID
            sw, sh = r0.get("max_TotalPixelMatrixColumns"), r0.get("max_TotalPixelMatrixRows")
            if local_dims and sw and sh:
                if (int(sw), int(sh)) == tuple(local_dims):
                    geom_note = f"level-0 dimensions verified equal ({sw}x{sh})"
                else:
                    geom_note = (f"DIMENSION MISMATCH: local {local_dims[0]}x{local_dims[1]} "
                                 f"vs source {sw}x{sh}")
            elif sw and sh:
                geom_note = f"source level-0 {sw}x{sh}; local slide not cached (not verified)"

        for arid, meta in IDC_COLLECTIONS.items():
            cand = patient[patient.analysis_result_id.astype(str) == arid]
            if not len(cand):
                rows.append(dict(
                    local_dataset=a["dataset"], local_asset_id=a["asset_id"],
                    slide_barcode=a["barcode"], source_collection=meta["label"],
                    source_identifier="", representation_type=meta["representation"],
                    source_url=meta["source_url"], source_size_bytes="",
                    license=meta["license"], exact_match_status="no_match",
                    download_status="not_applicable",
                    coordinate_notes="no annotation published for this case",
                ))
                continue

            for _, c in cand.iterrows():
                # Follow the segmentation's own reference to the series it was computed on.
                ref = seg[seg.SeriesInstanceUID == c.SeriesInstanceUID]
                ref_uid = ref.iloc[0].segmented_SeriesInstanceUID if len(ref) else None
                if ref_uid and src_uid and ref_uid == src_uid:
                    status = "exact_slide_match"
                    note = (f"segmentation references the source series of this slide"
                            + (f"; {geom_note}" if geom_note else ""))
                elif ref_uid and src_uid:
                    status = "different_slide_same_case"
                    note = "segmentation references a different slide of the same case"
                else:
                    status = "same_case_unverified"
                    note = "could not resolve the segmentation's referenced series"
                rows.append(dict(
                    local_dataset=a["dataset"], local_asset_id=a["asset_id"],
                    slide_barcode=a["barcode"], source_collection=meta["label"],
                    source_identifier=c.crdc_series_uuid,
                    representation_type=str(c.SeriesDescription or meta["representation"]),
                    source_url=f"s3://{c.aws_bucket}/{c.crdc_series_uuid}/"
                               if "aws_bucket" in c else meta["source_url"],
                    source_size_bytes=int(float(c.series_size_MB) * 1e6)
                                      if c.series_size_MB == c.series_size_MB else "",
                    license=meta["license"], exact_match_status=status,
                    download_status="pending", coordinate_notes=note,
                ))

        for t in TILE_COLLECTIONS:
            rows.append(dict(
                local_dataset=a["dataset"], local_asset_id=a["asset_id"],
                slide_barcode=a["barcode"], source_collection=t["label"],
                source_identifier="", representation_type=t["representation"],
                source_url=t["source_url"], source_size_bytes="", license=t["license"],
                exact_match_status="not_slide_registerable",
                download_status="not_attempted", coordinate_notes=t["note"],
            ))

    return rows


def main(argv=None):
    """Build the manifest and write it as TSV.

    Args:
        argv: Optional argument list (defaults to sys.argv).

    Returns:
        The path the manifest was written to.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp/spatial_annotation_manifest.tsv")
    ap.add_argument("--allow-download", action="store_true",
                    help="pull uncached slides from object storage to verify geometry")
    args = ap.parse_args(argv)

    from Database.database import SessionLocal
    session = SessionLocal()
    try:
        rows = build_rows(session, allow_download=args.allow_download)
    finally:
        session.close()

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
        w.writeheader()
        w.writerows(rows)

    matched = sum(1 for r in rows if r["exact_match_status"] == "exact_slide_match")
    logger.info(f"Wrote {len(rows)} manifest rows to {args.out} ({matched} exact slide matches).")
    return args.out


if __name__ == "__main__":
    sys.path.insert(0, "/shared")
    try:
        from logging_setup import configure_logging
        configure_logging()
    except ImportError:
        logging.basicConfig(level=logging.INFO)
    main()
