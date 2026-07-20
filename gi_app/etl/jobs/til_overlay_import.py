"""Import ONE published TIL map for ONE slide, with full provenance and verified geometry.

Source-data only. The published DICOM Segmentation object is downloaded byte-for-byte and
stored unchanged as an `annotation_source` asset. A PNG is generated purely so a browser can
draw it, registered separately as a `rendering_cache` derivative that points back at its
original. No value is invented, thresholded or re-scored: the PNG encodes the source values.

Refuses to register anything it cannot geometrically justify. If the segmentation does not
reference this exact slide, or its pixel spacing/origin cannot be reconciled with the slide's
own metadata, the import aborts and reports the incompatibility instead of guessing.

Registration
------------
The map is a uniform grid over the slide's total pixel matrix. Placing it on level 0 needs
only a scale (and an origin, which the source states explicitly):

    level0_px_per_map_px = seg_pixel_spacing_mm / slide_mm_per_pixel
    origin_px            = seg_origin_mm       / slide_mm_per_pixel

Both are written to `annotation_representations.transform_metadata` so the viewer places the
layer from recorded numbers rather than by stretching it to fit.

Usage (from the api container):
    docker exec gi_app-api-1 python /etl/jobs/til_overlay_import.py --asset-id 32
"""

import argparse
import hashlib
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COLLECTION = {
    "name": "TCGA-SBU-TIL-Maps",
    "provider": "Stony Brook University (Saltz lab), via NCI Imaging Data Commons",
    "source_url": "https://doi.org/10.5281/zenodo.16966285",
    "license": "CC BY 4.0",
    "version": "idc_v23",
    "method": "algorithmic",
    "origin": "published_derived",
    "citation": (
        "Bridge, C., Abousamra, S., Saltz, J., Gupta, R., Kurc, T., Zhang, Y., Zhao, T., "
        "Batiste, R., Samaras, D., Bremer, E., Shroyer, K. R., Nguyen, V., Singh, P., Hou, L., "
        "Van Arnam, J., Shmulevich, I., Rao, A. U. K., Lazar, A. J., Sharma, A., … Fedorov, A. "
        "(2025). TCGA-SBU-TIL-Maps: AI-derived Tumor Infiltrating Lymphocyte maps for the TCGA "
        "collections. Zenodo. https://doi.org/10.5281/zenodo.16966285 — produced with the "
        "algorithms of Saltz et al., Cell Reports 23(1):181-193 (2018), "
        "doi:10.1016/j.celrep.2018.03.086 and Abousamra et al., Front. Oncol. 12:806603 (2022), "
        "doi:10.3389/fonc.2022.806603"
    ),
    # Wording checked against the sources (2026-07-20). Saltz et al. 2018 trained the model on
    # ~20,876 pathologist-annotated patches, had three pathologists validate it, and had them
    # edit 10-20 WSIs per cancer type (~3% of that study's maps) to calibrate thresholds applied
    # globally. But THIS collection is the 2025 regeneration across 23 TCGA collections, which
    # makes no pathologist-review claim and does not record which slides were reviewed. So the
    # method is pathologist-trained and validated; per-slide review is unstated, not denied.
    "description": (
        "Published algorithmic result. Tumour-infiltrating-lymphocyte probability from a "
        "convolutional neural network over a uniform grid of 50x50 micron patches. The method "
        "was trained and validated on pathologist-annotated data; these maps are model output, "
        "and the release does not state whether any individual slide was reviewed."
    ),
}

# Expected DICOM slide orientation: rows advance +y, columns advance +x, i.e. the map's
# (0,0) is the slide's top-left, matching OpenSlide level-0 origin.
EXPECTED_ORIENTATION = [0.0, -1.0, 0.0, -1.0, 0.0, 0.0]


class IncompatibleSource(RuntimeError):
    """Raised when a source annotation cannot be registered against the slide."""


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_til_series(barcode, want="fractional"):
    """Find the published TIL segmentation series for one slide barcode.

    Args:
        barcode: Full TCGA slide barcode, e.g. 'TCGA-CA-5255-01Z-00-DX1'.
        want: 'fractional' (probability) or 'binary'.

    Returns:
        A dict describing the matched series, or None when nothing matches.

    Raises:
        IncompatibleSource: If a candidate exists but references a different slide.
    """
    sys.path.insert(0, os.path.dirname(__file__))
    from spatial_manifest import _fetch_index, split_barcode

    idx = _fetch_index("idc_index.parquet")
    seg = _fetch_index("seg_index.parquet")
    case_bc, token = split_barcode(barcode)

    patient = idx[idx.PatientID == case_bc]
    src = patient[patient.Modality == "SM"]
    if token:
        src = src[src.SeriesDescription.astype(str).str.contains(token, na=False)]
    if not len(src):
        return None
    src_uid = src.iloc[0].SeriesInstanceUID

    cand = patient[patient.analysis_result_id.astype(str) == "tcga_sbu_til_maps"]
    keyword = "Fractional" if want == "fractional" else "Binary"
    cand = cand[cand.SeriesDescription.astype(str).str.contains(keyword, na=False)]
    if not len(cand):
        return None

    for _, c in cand.iterrows():
        ref = seg[seg.SeriesInstanceUID == c.SeriesInstanceUID]
        if not len(ref):
            continue
        if ref.iloc[0].segmented_SeriesInstanceUID != src_uid:
            continue  # same case, different slide — not our slide
        return {
            "crdc_series_uuid": c.crdc_series_uuid,
            "series_uid": c.SeriesInstanceUID,
            "description": str(c.SeriesDescription),
            "referenced_series_uid": src_uid,
            "algorithm": str(ref.iloc[0].AlgorithmName),
            "segmentation_type": str(ref.iloc[0].SegmentationType),
            "aws_prefix": f"s3://idc-open-data/{c.crdc_series_uuid}/",
        }
    raise IncompatibleSource(
        f"TIL maps exist for case {case_bc} but none reference slide {barcode}."
    )


def download_series_object(crdc_series_uuid, dest_dir):
    """Download the single DICOM object of a published series over public HTTPS.

    Args:
        crdc_series_uuid: The IDC series uuid (also the storage prefix).
        dest_dir: Local directory to write into.

    Returns:
        The local path of the downloaded file.

    Raises:
        IncompatibleSource: If the series does not contain exactly one object.
    """
    import re
    import urllib.request

    base = "https://idc-open-data.s3.amazonaws.com"
    listing = urllib.request.urlopen(
        f"{base}/?list-type=2&prefix={crdc_series_uuid}/", timeout=120).read().decode()
    keys = re.findall(r"<Key>(.*?)</Key>", listing)
    if len(keys) != 1:
        raise IncompatibleSource(
            f"expected exactly one object under {crdc_series_uuid}/, found {len(keys)}")
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, os.path.basename(keys[0]))
    urllib.request.urlretrieve(f"{base}/{keys[0]}", path)
    logger.info(f"Downloaded {os.path.getsize(path)} bytes -> {path}")
    return path


def read_segmentation(path):
    """Read a DICOM SEG and return its array plus the geometry needed to place it.

    Args:
        path: Local path of the DICOM Segmentation file.

    Returns:
        A dict with the pixel array, grid size, pixel spacing, origin and value range.
    """
    import numpy as np
    import pydicom

    d = pydicom.dcmread(path)
    shared = d.SharedFunctionalGroupsSequence[0]
    spacing = None
    if "PixelMeasuresSequence" in shared:
        spacing = [float(v) for v in shared.PixelMeasuresSequence[0].PixelSpacing]
    orientation = None
    if "PlaneOrientationSequence" in shared:
        orientation = [float(v) for v in shared.PlaneOrientationSequence[0].ImageOrientationSlide]
    origin = (0.0, 0.0)
    if "TotalPixelMatrixOriginSequence" in d:
        o = d.TotalPixelMatrixOriginSequence[0]
        origin = (float(getattr(o, "XOffsetInSlideCoordinateSystem", 0.0)),
                  float(getattr(o, "YOffsetInSlideCoordinateSystem", 0.0)))
    arr = np.asarray(d.pixel_array)
    if arr.ndim == 3:  # single-frame files may still carry a frame axis
        arr = arr[0]
    return {
        "array": arr,
        "rows": int(d.Rows), "columns": int(d.Columns),
        "segmentation_type": str(d.SegmentationType),
        "pixel_spacing_mm": spacing,
        "orientation": orientation,
        "origin_mm": origin,
        "maximum_fractional_value": int(getattr(d, "MaximumFractionalValue", 1)),
        "referenced_series_uid": (d.ReferencedSeriesSequence[0].SeriesInstanceUID
                                  if "ReferencedSeriesSequence" in d else None),
        "segment_labels": [str(s.SegmentLabel) for s in d.SegmentSequence],
        "algorithm_names": [str(getattr(s, "SegmentAlgorithmName", "")) for s in d.SegmentSequence],
    }


def compute_transform(seg, slide_w, slide_h, mpp_x, mpp_y):
    """Reconcile the segmentation grid with the slide's level-0 pixel grid.

    Args:
        seg: The dict returned by `read_segmentation`.
        slide_w: Slide level-0 width in pixels.
        slide_h: Slide level-0 height in pixels.
        mpp_x: Slide microns per pixel, x.
        mpp_y: Slide microns per pixel, y.

    Returns:
        A transform dict describing where the grid sits in level-0 pixels.

    Raises:
        IncompatibleSource: If the geometry cannot be reconciled.
    """
    if not seg["pixel_spacing_mm"]:
        raise IncompatibleSource("segmentation declares no PixelSpacing — cannot scale it")
    if not (mpp_x and mpp_y):
        raise IncompatibleSource(
            "slide declares no microns-per-pixel, so the map's physical spacing cannot be "
            "converted to level-0 pixels")

    row_mm, col_mm = seg["pixel_spacing_mm"]      # DICOM order: [row spacing, column spacing]
    slide_mm_x, slide_mm_y = mpp_x / 1000.0, mpp_y / 1000.0
    scale_x = col_mm / slide_mm_x
    scale_y = row_mm / slide_mm_y

    ox_mm, oy_mm = seg["origin_mm"]
    offset_x = ox_mm / slide_mm_x
    offset_y = oy_mm / slide_mm_y

    covered_w = seg["columns"] * scale_x + offset_x
    covered_h = seg["rows"] * scale_y + offset_y

    # The grid is built by tiling whole patches, so it may fall short of the slide edge by
    # up to one patch. Anything beyond that means the two are not the same image.
    tol_x, tol_y = scale_x * 1.5, scale_y * 1.5
    if not (slide_w - tol_x <= covered_w <= slide_w + tol_x):
        raise IncompatibleSource(
            f"map width {covered_w:.0f}px does not match slide width {slide_w}px "
            f"(tolerance {tol_x:.0f}px)")
    if not (slide_h - tol_y <= covered_h <= slide_h + tol_y):
        raise IncompatibleSource(
            f"map height {covered_h:.0f}px does not match slide height {slide_h}px "
            f"(tolerance {tol_y:.0f}px)")

    orientation_ok = (seg["orientation"] is None
                      or [round(v, 3) for v in seg["orientation"]] == EXPECTED_ORIENTATION
                      or [round(v, 3) for v in seg["orientation"]] == [1.0, 0, 0, 0, 1.0, 0])
    if not orientation_ok:
        raise IncompatibleSource(
            f"unexpected ImageOrientationSlide {seg['orientation']} — the map may be flipped "
            "or rotated relative to the slide; refusing to register it")

    return {
        "coordinate_space": "level_0_pixels",
        "level": 0,
        "slide_width_px": slide_w,
        "slide_height_px": slide_h,
        "slide_mpp_x": mpp_x,
        "slide_mpp_y": mpp_y,
        "grid_columns": seg["columns"],
        "grid_rows": seg["rows"],
        "pixel_spacing_mm": [row_mm, col_mm],
        "level0_px_per_map_px_x": scale_x,
        "level0_px_per_map_px_y": scale_y,
        "offset_x_px": offset_x,
        "offset_y_px": offset_y,
        # Extent the layer must occupy on level 0 — the viewer uses exactly these.
        "extent_x_px": seg["columns"] * scale_x,
        "extent_y_px": seg["rows"] * scale_y,
        "orientation": seg["orientation"],
        "source_origin_mm": list(seg["origin_mm"]),
    }


def render_png(seg):
    """Render the source values as an RGBA PNG for browser display.

    The alpha channel carries the source value; nothing is thresholded or smoothed, so the
    drawn image is a faithful re-encoding rather than a new result.

    Args:
        seg: The dict returned by `read_segmentation`.

    Returns:
        A (png_bytes, min_value, max_value) tuple, values on the source's own scale.
    """
    import numpy as np
    from PIL import Image

    a = seg["array"].astype(np.float32)
    if seg["segmentation_type"].upper() == "FRACTIONAL":
        maxv = float(seg["maximum_fractional_value"] or 255)
        prob = np.clip(a / maxv, 0.0, 1.0)
    else:
        prob = np.clip(a, 0.0, 1.0)

    h, w = prob.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    # Blue -> magenta ramp with probability; alpha scales with the value so low-TIL areas
    # stay readable over the tissue underneath.
    rgba[..., 0] = (255 * prob).astype(np.uint8)
    rgba[..., 1] = (40 * prob).astype(np.uint8)
    rgba[..., 2] = (255 * (0.35 + 0.65 * prob)).astype(np.uint8)
    rgba[..., 3] = (220 * prob).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), float(prob.min()), float(prob.max())


def import_overlay(asset_id, want="fractional", target="local"):
    """Download, verify and register one published TIL map for one slide asset.

    Args:
        asset_id: The WSI data_assets.asset_id to annotate.
        want: 'fractional' (probability map) or 'binary'.
        target: Storage target for the stored files.

    Returns:
        A summary dict describing what was registered.

    Raises:
        IncompatibleSource: If the source cannot be verified against the slide.
    """
    import openslide
    import storage
    from Database.database import SessionLocal
    from Database.models import (Annotation, AnnotationRepresentation, AnnotationSet,
                                 DataAsset, Slide)

    session = SessionLocal()
    try:
        asset = session.query(DataAsset).filter_by(asset_id=asset_id).one_or_none()
        if asset is None or asset.asset_type != "wsi":
            raise IncompatibleSource(f"asset {asset_id} is not a registered WSI")
        slide = session.query(Slide).filter_by(slide_id=asset.slide_id).one_or_none()
        if slide is None:
            raise IncompatibleSource(f"asset {asset_id} is not linked to a slides row")
        barcode = slide.submitter_id
        logger.info(f"Slide {barcode} (asset {asset_id})")

        match = find_til_series(barcode, want=want)
        if match is None:
            raise IncompatibleSource(f"no published TIL map references slide {barcode}")
        logger.info(f"Matched {match['description']} ({match['crdc_series_uuid']})")

        # --- source geometry, straight from the slide itself -------------------------
        cache = os.environ.get("SLIDE_CACHE_DIR", "/tmp/slide-cache")
        ext = os.path.splitext(asset.uri)[1] or ".svs"
        local_slide = os.path.join(cache, f"{asset_id}{ext}")
        if not os.path.exists(local_slide):
            os.makedirs(cache, exist_ok=True)
            storage.download_file(asset.uri, local_slide)
        osr = openslide.OpenSlide(local_slide)
        slide_w, slide_h = osr.dimensions
        mpp_x = float(osr.properties.get(openslide.PROPERTY_NAME_MPP_X) or 0) or None
        mpp_y = float(osr.properties.get(openslide.PROPERTY_NAME_MPP_Y) or 0) or None
        osr.close()

        work = os.path.join("/tmp", "til-import", str(asset_id))
        src_path = download_series_object(match["crdc_series_uuid"], work)
        seg = read_segmentation(src_path)

        if seg["referenced_series_uid"] != match["referenced_series_uid"]:
            raise IncompatibleSource(
                "downloaded segmentation references series "
                f"{seg['referenced_series_uid']}, not this slide's "
                f"{match['referenced_series_uid']}")
        logger.info("Verified: segmentation references THIS slide's series.")

        transform = compute_transform(seg, slide_w, slide_h, mpp_x, mpp_y)
        logger.info(f"Geometry reconciled: {transform['grid_columns']}x{transform['grid_rows']} "
                    f"grid, {transform['level0_px_per_map_px_x']:.2f} level-0 px per map px")

        # --- store the original, unmodified ------------------------------------------
        now = datetime.now(timezone.utc)
        base = f"TIL_{barcode}_{want}"
        src_uri = storage.build_uri("bronze", "annotations", "TCGA-SBU-TIL-Maps",
                                    f"{base}.dcm", target=target)
        storage.put_file(src_path, src_uri, target=target)
        src_asset = session.query(DataAsset).filter_by(uri=src_uri).one_or_none()
        fields = dict(
            dataset_id=asset.dataset_id, slide_id=asset.slide_id,
            asset_type="annotation_source", layer="bronze", format="dcm",
            md5=_md5(src_path), size_bytes=os.path.getsize(src_path),
            source_file_id=match["crdc_series_uuid"], source_uri=match["aws_prefix"],
        )
        if src_asset is None:
            src_asset = DataAsset(uri=src_uri, created_at=now, **fields)
            session.add(src_asset)
        else:
            for k, v in fields.items():
                setattr(src_asset, k, v)
        session.flush()

        # --- rendering derivative -----------------------------------------------------
        png, vmin, vmax = render_png(seg)
        png_path = os.path.join(work, f"{base}.png")
        with open(png_path, "wb") as f:
            f.write(png)
        png_uri = storage.build_uri("silver", "annotations", "TCGA-SBU-TIL-Maps",
                                    f"{base}.png", target=target)
        storage.put_file(png_path, png_uri, target=target)
        png_asset = session.query(DataAsset).filter_by(uri=png_uri).one_or_none()
        png_fields = dict(
            dataset_id=asset.dataset_id, slide_id=asset.slide_id,
            asset_type="rendering_cache", layer="silver", format="png",
            md5=_md5(png_path), size_bytes=os.path.getsize(png_path),
            source_file_id=match["crdc_series_uuid"], source_uri=match["aws_prefix"],
            derived_from_asset_id=src_asset.asset_id,
        )
        if png_asset is None:
            png_asset = DataAsset(uri=png_uri, created_at=now, **png_fields)
            session.add(png_asset)
        else:
            for k, v in png_fields.items():
                setattr(png_asset, k, v)
        session.flush()

        # --- annotation set / annotation / representations ----------------------------
        aset = (session.query(AnnotationSet)
                .filter_by(dataset_id=asset.dataset_id, name=COLLECTION["name"],
                           version=COLLECTION["version"]).one_or_none())
        if aset is None:
            aset = AnnotationSet(
                dataset_id=asset.dataset_id, name=COLLECTION["name"],
                provider=COLLECTION["provider"], source_url=COLLECTION["source_url"],
                citation=COLLECTION["citation"], license=COLLECTION["license"],
                version=COLLECTION["version"], method=COLLECTION["method"],
                origin=COLLECTION["origin"], description=COLLECTION["description"],
                retrieved_at=now, created_at=now,
            )
            session.add(aset)
            session.flush()

        ann = (session.query(Annotation)
               .filter_by(annotation_set_id=aset.annotation_set_id,
                          source_annotation_id=match["series_uid"]).one_or_none())
        rep_type = ("probability_map" if seg["segmentation_type"].upper() == "FRACTIONAL"
                    else "binary_mask")
        ann_fields = dict(
            case_id=slide.case_id, target_asset_id=asset.asset_id, scope="region",
            annotation_type="TIL", label=match["description"],
            category="Tumour-infiltrating lymphocytes",
            classification=seg["segmentation_type"].upper(),
            value_text=json.dumps(seg["segment_labels"]),
            units="probability" if rep_type == "probability_map" else "class",
            # "not_stated", not "not_reviewed": the release is silent on per-slide review, and
            # asserting it did NOT happen would claim more than the source supports.
            review_status="not_stated",
            notes=(f"{match['algorithm']}. Published algorithmic result over a "
                   f"{transform['grid_columns']}x{transform['grid_rows']} grid of 50x50 "
                   "micron patches. Per-slide pathologist review is not stated by the source."),
            created_at=now,
        )
        if ann is None:
            ann = Annotation(annotation_set_id=aset.annotation_set_id,
                             source_annotation_id=match["series_uid"], **ann_fields)
            session.add(ann)
        else:
            for k, v in ann_fields.items():
                setattr(ann, k, v)
        session.flush()

        for asset_row, rtype in ((src_asset, rep_type), (png_asset, "rendering_derivative")):
            rep = (session.query(AnnotationRepresentation)
                   .filter_by(annotation_id=ann.annotation_id,
                              asset_id=asset_row.asset_id).one_or_none())
            rep_fields = dict(
                representation_type=rtype,
                coordinate_space=transform["coordinate_space"],
                width=transform["grid_columns"], height=transform["grid_rows"],
                level=0, transform_metadata=transform,
                minimum_value=vmin, maximum_value=vmax, created_at=now,
            )
            if rep is None:
                session.add(AnnotationRepresentation(
                    annotation_id=ann.annotation_id, asset_id=asset_row.asset_id, **rep_fields))
            else:
                for k, v in rep_fields.items():
                    setattr(rep, k, v)

        session.commit()
        summary = {
            "slide_barcode": barcode, "wsi_asset_id": asset_id,
            "annotation_set_id": aset.annotation_set_id, "annotation_id": ann.annotation_id,
            "source_asset_id": src_asset.asset_id, "render_asset_id": png_asset.asset_id,
            "representation_type": rep_type, "grid": [transform["grid_columns"],
                                                      transform["grid_rows"]],
            "scale_px": [transform["level0_px_per_map_px_x"],
                         transform["level0_px_per_map_px_y"]],
            "source_uri": match["aws_prefix"],
        }
        logger.info(f"Registered: {json.dumps(summary, indent=2)}")
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main(argv=None):
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset-id", type=int, required=True)
    ap.add_argument("--variant", choices=["fractional", "binary"], default="fractional")
    ap.add_argument("--target", default="local")
    args = ap.parse_args(argv)
    try:
        import_overlay(args.asset_id, want=args.variant, target=args.target)
        return 0
    except IncompatibleSource as e:
        logger.error(f"REFUSED — source cannot be registered against this slide: {e}")
        return 2


if __name__ == "__main__":
    sys.path.insert(0, "/shared")
    try:
        from logging_setup import configure_logging
        configure_logging()
    except ImportError:
        logging.basicConfig(level=logging.INFO)
    sys.exit(main())
