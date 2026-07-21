"""Invariants for the pathology-report assets.

A report is the first case-scoped entry in `data_assets` — every other asset type reaches
its patient through a slide. These encode what that link must satisfy: a report always
names a real case, never pretends to be a slide artefact, and is reachable from the two
read paths the app uses.

Read-only. Nothing here inserts or deletes an asset.
"""

from sqlalchemy import text


def _scalar(session, sql, **params):
    return session.execute(text(sql), params).scalar()


def test_pathology_report_is_an_allowed_asset_type(session):
    """The CHECK must admit the type, or ingest fails at insert time."""
    allowed = _scalar(session, """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname = 'ck_data_assets_asset_type'
    """)
    assert allowed is not None, "asset_type CHECK constraint is missing"
    assert "pathology_report" in allowed


def test_every_report_names_a_real_case(session):
    """A report with no resolvable case is unreachable — nothing could ever display it."""
    orphans = _scalar(session, """
        SELECT count(*) FROM data_assets d
        LEFT JOIN cases c ON c.case_id = d.case_id
        WHERE d.asset_type = 'pathology_report'
          AND (d.case_id IS NULL OR c.case_id IS NULL)
    """)
    assert orphans == 0


def test_reports_are_not_slide_scoped(session):
    """A report describes a specimen, not one slide; a slide_id here means a mis-ingest."""
    mislinked = _scalar(session, """
        SELECT count(*) FROM data_assets
        WHERE asset_type = 'pathology_report' AND slide_id IS NOT NULL
    """)
    assert mislinked == 0


def test_report_case_belongs_to_the_assets_dataset(session):
    """The case link must stay inside the asset's own dataset.

    Barcodes are unique per project but not across them, so a barcode-keyed lookup that
    forgot to scope by dataset would silently attach a report to the wrong patient.
    """
    crossed = _scalar(session, """
        SELECT count(*) FROM data_assets d
        JOIN cases c ON c.case_id = d.case_id
        WHERE d.asset_type = 'pathology_report' AND c.dataset_id <> d.dataset_id
    """)
    assert crossed == 0


def test_reports_are_registered_as_pdfs_in_bronze(session):
    """Format and layer drive the signed link's content type and the storage path."""
    wrong = _scalar(session, """
        SELECT count(*) FROM data_assets
        WHERE asset_type = 'pathology_report'
          AND (format <> 'pdf' OR layer <> 'bronze' OR uri IS NULL)
    """)
    assert wrong == 0


def test_no_duplicate_report_uris(session):
    """Ingest is idempotent by URI, so a repeated URI means re-runs are creating rows."""
    dupes = _scalar(session, """
        SELECT count(*) FROM (
            SELECT uri FROM data_assets WHERE asset_type = 'pathology_report'
            GROUP BY uri HAVING count(*) > 1
        ) d
    """)
    assert dupes == 0


def test_case_endpoint_exposes_reports(session, client):
    """The per-patient read path must surface the report, or the viewer has nothing to open."""
    case_id = _scalar(session, """
        SELECT case_id FROM data_assets
        WHERE asset_type = 'pathology_report' AND case_id IS NOT NULL LIMIT 1
    """)
    if case_id is None:
        import pytest
        pytest.skip("no pathology reports ingested")

    body = client.get(f"/cases/{case_id}").json()
    assert body["pathology_reports"], "case has a report asset but the endpoint returned none"
    assert body["pathology_reports"][0]["asset_id"] is not None


def test_ingest_retires_on_manifest_not_on_local_files(tmp_path):
    """A report absent from local disk but still in the manifest must keep its registration.

    The PDFs are only needed to *upload*; once in object storage the local copies are
    disposable (42 MB for COAD). An ingest that retired assets whose local file is missing
    would deregister the whole cohort on any machine that had cleared them, orphaning the
    stored objects — so retirement must key off manifest membership, not upload success.
    """
    import os

    import pytest

    path = "/etl/ingest/report_ingest.py"
    if not os.path.exists(path):
        pytest.skip("etl sources not mounted in this container")
    src = open(path).read()

    assert "in_manifest" in src, "retirement no longer tracks manifest membership"
    # The retirement branch must test manifest membership, not the uploaded set.
    retire = src.split("for uri, a in existing.items():")[1]
    assert "not in in_manifest" in retire, "retirement keys off uploads, not the manifest"
    assert "not in seen" not in retire, "retirement regressed to keying off uploads"


def test_cohort_endpoint_exposes_reports(session, client):
    """The cohort table needs the report list to render its per-patient button.

    Asserted on both axes because they are not the same number: a few cases carry more
    than one report file, so `cases with a report` < `report assets`. Comparing only the
    first would hide a payload that dropped the extra files.
    """
    row = session.execute(text("""
        SELECT d.dataset_id,
               count(*) AS n_reports,
               count(DISTINCT d.case_id) AS n_cases
        FROM data_assets d
        WHERE d.asset_type = 'pathology_report' GROUP BY d.dataset_id LIMIT 1
    """)).mappings().first()
    if row is None:
        import pytest
        pytest.skip("no pathology reports ingested")

    cases = client.get(f"/datasets/{row['dataset_id']}/cases").json()
    with_reports = [c for c in cases if c["pathology_reports"]]
    assert len(with_reports) == row["n_cases"], "cohort payload lost a case's reports"
    assert sum(len(c["pathology_reports"]) for c in with_reports) == row["n_reports"], \
        "cohort payload lost a report file from a multi-report case"
