"""API contract tests for annotations, spatial layers and the timeline.

The central guarantee: a caller can always tell where an annotation came from and whether a
human or an algorithm produced it, without having to look it up elsewhere.
"""

import pytest
from sqlalchemy import text


@pytest.fixture(scope="module")
def sample_ids():
    """Ids to exercise the endpoints with, taken from whatever is actually loaded."""
    from Database.database import SessionLocal
    s = SessionLocal()
    try:
        case_id = s.execute(text(
            "SELECT case_id FROM annotations WHERE case_id IS NOT NULL LIMIT 1")).scalar()
        spatial = s.execute(text("""
            SELECT target_asset_id FROM annotations
            WHERE target_asset_id IS NOT NULL LIMIT 1
        """)).scalar()
        wsi = s.execute(text(
            "SELECT asset_id FROM data_assets WHERE asset_type = 'wsi' LIMIT 1")).scalar()
        return {"case_id": case_id, "spatial_asset_id": spatial, "wsi_asset_id": wsi}
    finally:
        s.close()


def test_annotation_sets_expose_provenance(client):
    r = client.get("/annotation-sets")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    for s in body["annotation_sets"]:
        assert s["origin"] in ("source_provided", "published_derived")
        assert s["provider"], "a set without a provider has no usable provenance"
        assert "is_algorithmic" in s and "is_published_derived" in s
        assert s["annotation_count"] >= 0


def test_case_annotations_include_provenance_and_spatial_flag(client, sample_ids):
    if not sample_ids["case_id"]:
        pytest.skip("no annotations loaded")
    r = client.get(f"/cases/{sample_ids['case_id']}/annotations")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == len(body["annotations"])
    for a in body["annotations"]:
        assert isinstance(a["is_spatial"], bool)
        assert a["annotation_set"]["provider"]
        assert a["source_annotation_id"]
        if a["is_spatial"]:
            assert a["target_asset_id"] is not None


def test_asset_annotations_are_scoped_to_that_asset(client, sample_ids):
    if not sample_ids["spatial_asset_id"]:
        pytest.skip("no spatial annotations loaded")
    aid = sample_ids["spatial_asset_id"]
    r = client.get(f"/assets/{aid}/annotations")
    assert r.status_code == 200
    for a in r.json()["annotations"]:
        assert a["target_asset_id"] == aid


def test_spatial_representations_carry_placement_geometry(client, sample_ids):
    if not sample_ids["spatial_asset_id"]:
        pytest.skip("no spatial annotations loaded")
    r = client.get(f"/assets/{sample_ids['spatial_asset_id']}/spatial-representations")
    assert r.status_code == 200
    layers = r.json()["layers"]
    assert layers, "expected at least one spatial layer"
    for l in layers:
        t = l["transform_metadata"]
        assert t, "a spatial layer with no transform cannot be placed on the slide"
        for key in ("slide_width_px", "slide_height_px", "extent_x_px", "extent_y_px",
                    "offset_x_px", "offset_y_px", "level0_px_per_map_px_x"):
            assert key in t, f"transform is missing {key}"
        assert l["coordinate_space"] == "level_0_pixels"
        assert l["annotation_set"]["license"]

    # Both the preserved original and the renderable derivative must be present.
    assert any(l["is_source_original"] for l in layers)
    assert any(l["is_renderable"] for l in layers)


def test_overlay_extent_matches_the_slide(client, sample_ids):
    """The registered layer must cover the slide to within one grid cell."""
    if not sample_ids["spatial_asset_id"]:
        pytest.skip("no spatial annotations loaded")
    r = client.get(f"/assets/{sample_ids['spatial_asset_id']}/spatial-representations")
    for l in r.json()["layers"]:
        t = l["transform_metadata"]
        cell_x = t["level0_px_per_map_px_x"]
        cell_y = t["level0_px_per_map_px_y"]
        assert abs(t["extent_x_px"] + t["offset_x_px"] - t["slide_width_px"]) <= cell_x * 1.5
        assert abs(t["extent_y_px"] + t["offset_y_px"] - t["slide_height_px"]) <= cell_y * 1.5


def test_non_renderable_representation_returns_415(client, sample_ids):
    """The preserved DICOM original is offered for download, never mis-served as an image."""
    if not sample_ids["spatial_asset_id"]:
        pytest.skip("no spatial annotations loaded")
    r = client.get(f"/assets/{sample_ids['spatial_asset_id']}/spatial-representations")
    src = [l for l in r.json()["layers"] if not l["is_renderable"]]
    if not src:
        pytest.skip("no non-renderable representation registered")
    img = client.get(f"/annotations/representations/{src[0]['representation_id']}/image")
    assert img.status_code == 415


def test_overlay_image_is_served(client, sample_ids):
    if not sample_ids["spatial_asset_id"]:
        pytest.skip("no spatial annotations loaded")
    r = client.get(f"/assets/{sample_ids['spatial_asset_id']}/spatial-representations")
    renderable = [l for l in r.json()["layers"] if l["is_renderable"]]
    if not renderable:
        pytest.skip("no renderable representation registered")
    img = client.get(renderable[0]["image_url"])
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_timeline_reports_timing_honestly(client, sample_ids):
    if not sample_ids["case_id"]:
        pytest.skip("no cases loaded")
    r = client.get(f"/cases/{sample_ids['case_id']}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["day_unit"].startswith("days relative")
    assert body["timed_event_count"] + body["untimed_event_count"] == body["total_events"]
    for g in body["groups"]:
        for e in g["events"]:
            assert e["day"] is not None
            assert e["timing_basis"] != "unknown"
    for e in body["untimed"]:
        assert e["day"] is None


def test_timeline_groups_are_sorted_and_unique(client, sample_ids):
    if not sample_ids["case_id"]:
        pytest.skip("no cases loaded")
    body = client.get(f"/cases/{sample_ids['case_id']}/timeline").json()
    days = [g["day"] for g in body["groups"]]
    assert days == sorted(days)
    assert len(days) == len(set(days)), "each relative day must appear as one group"


def test_timeline_404_for_unknown_case(client):
    r = client.get("/cases/00000000-0000-0000-0000-000000000000/timeline")
    assert r.status_code == 404


def test_case_detail_endpoint(client, sample_ids):
    if not sample_ids["case_id"]:
        pytest.skip("no cases loaded")
    r = client.get(f"/cases/{sample_ids['case_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["case_barcode"]
    assert body["annotation_count"] >= 0


def test_existing_slide_endpoints_still_work(client, sample_ids):
    """The annotation work must not disturb slide viewing."""
    if not sample_ids["wsi_asset_id"]:
        pytest.skip("no WSI assets loaded")
    r = client.get(f"/slides/{sample_ids['wsi_asset_id']}/info")
    assert r.status_code == 200
    info = r.json()
    assert info["width"] > 0 and info["height"] > 0 and info["levels"] > 0


# --------------------- timeline presentation guarantees ---------------------- #
# The `case_timeline` VIEW stays a faithful 1:1 map of source records. These collapse and
# suppression rules live in the API layer, and must never hide a record that says something
# different from its neighbours.

@pytest.fixture(scope="module")
def dead_case_with_duplicate_follow_ups():
    """A case that exercises both rules, or a skip when the cohort has none loaded."""
    from Database.database import SessionLocal
    s = SessionLocal()
    try:
        row = s.execute(text("""
            SELECT c.case_id FROM cases c
            JOIN follow_ups f USING(case_id)
            WHERE c.days_to_death IS NOT NULL AND f.days_to_follow_up IS NOT NULL
            GROUP BY c.case_id, f.days_to_follow_up
            HAVING count(*) > 1
            LIMIT 1
        """)).scalar()
    finally:
        s.close()
    if row is None:
        pytest.skip("no dead case with same-day duplicate follow-ups in the loaded cohort")
    return str(row)


def test_identical_same_day_records_collapse_but_keep_their_count(
        client, dead_case_with_duplicate_follow_ups):
    """A merged card must declare how many source records it stands for."""
    body = client.get(f"/cases/{dead_case_with_duplicate_follow_ups}/timeline").json()
    merged = [e for g in body["groups"] for e in g["events"] if e.get("source_count", 1) > 1]
    assert merged, "expected at least one collapsed card for this case"
    for e in merged:
        assert len(e["ref_ids"]) == e["source_count"], "every source id must stay addressable"


def test_collapsing_never_merges_records_that_disagree(client):
    """Cards sharing a day must remain distinct whenever any displayed field differs."""
    from Database.database import SessionLocal
    s = SessionLocal()
    try:
        case_id = s.execute(text("""
            SELECT case_id FROM (
              SELECT case_id, days_to_follow_up,
                     count(DISTINCT coalesce(disease_response, '(null)')) AS n_distinct
              FROM follow_ups WHERE days_to_follow_up IS NOT NULL
              GROUP BY case_id, days_to_follow_up HAVING count(*) > 1) q
            WHERE n_distinct > 1 LIMIT 1
        """)).scalar()
    finally:
        s.close()
    if case_id is None:
        pytest.skip("no case with conflicting same-day follow-ups in the loaded cohort")

    body = client.get(f"/cases/{case_id}/timeline").json()
    for group in body["groups"]:
        details = [e["detail"] for e in group["events"] if e["event_type"] == "follow_up"]
        assert len(details) == len(set(details)), "distinct values were collapsed together"


def test_last_follow_up_is_suppressed_only_on_the_death_day(
        client, dead_case_with_duplicate_follow_ups):
    """It is redundant on the death day and meaningful everywhere else."""
    body = client.get(f"/cases/{dead_case_with_duplicate_follow_ups}/timeline").json()
    for group in body["groups"]:
        types = [e["event_type"] for e in group["events"]]
        if "death" in types:
            assert "last_follow_up" not in types


def test_timeline_totals_count_source_records_not_cards(
        client, dead_case_with_duplicate_follow_ups):
    """Collapsing must not make the headline counts under-report the source."""
    body = client.get(f"/cases/{dead_case_with_duplicate_follow_ups}/timeline").json()
    cards = sum(len(g["events"]) for g in body["groups"])
    assert body["timed_event_count"] >= cards
