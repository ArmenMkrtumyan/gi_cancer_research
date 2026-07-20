"""Database invariants for the generalized annotation system.

These encode the acceptance criteria for the schema: no annotation may exist without
provenance, no spatial annotation may float free of a slide, no representation may point at
a file that is not registered, and the migration must be safe to run again.
"""

from sqlalchemy import text


def _scalar(session, sql, **params):
    return session.execute(text(sql), params).scalar()


def test_every_annotation_belongs_to_a_set(session):
    """Provenance is mandatory: an annotation with no set has no traceable origin."""
    assert _scalar(session, "SELECT count(*) FROM annotations WHERE annotation_set_id IS NULL") == 0


def test_no_orphaned_annotation_sets(session):
    """Every annotation's set must actually exist."""
    orphans = _scalar(session, """
        SELECT count(*) FROM annotations a
        LEFT JOIN annotation_sets s ON s.annotation_set_id = a.annotation_set_id
        WHERE s.annotation_set_id IS NULL
    """)
    assert orphans == 0


def test_no_orphaned_case_links(session):
    """A non-null case_id must resolve to a real case."""
    orphans = _scalar(session, """
        SELECT count(*) FROM annotations a
        LEFT JOIN cases c ON c.case_id = a.case_id
        WHERE a.case_id IS NOT NULL AND c.case_id IS NULL
    """)
    assert orphans == 0


def test_spatial_annotations_target_an_exact_wsi(session):
    """A spatial annotation must name the exact slide it sits on, and that slide must be a WSI."""
    missing = _scalar(session, """
        SELECT count(*) FROM annotations
        WHERE scope IN ('region','nucleus','patch','tile') AND target_asset_id IS NULL
    """)
    assert missing == 0

    not_wsi = _scalar(session, """
        SELECT count(*) FROM annotations a
        JOIN data_assets d ON d.asset_id = a.target_asset_id
        WHERE a.scope IN ('region','nucleus','patch','tile') AND d.asset_type <> 'wsi'
    """)
    assert not_wsi == 0


def test_representations_point_at_registered_assets(session):
    """Every representation must reference a data_assets row."""
    orphans = _scalar(session, """
        SELECT count(*) FROM annotation_representations r
        LEFT JOIN data_assets d ON d.asset_id = r.asset_id
        WHERE d.asset_id IS NULL
    """)
    assert orphans == 0


def test_representations_belong_to_spatial_annotations(session):
    """A representation only makes sense for an annotation that has a location."""
    bad = _scalar(session, """
        SELECT count(*) FROM annotation_representations r
        JOIN annotations a ON a.annotation_id = r.annotation_id
        WHERE a.scope NOT IN ('region','nucleus','patch','tile','slide')
    """)
    assert bad == 0


def test_source_ids_unique_within_a_set(session):
    """Re-importing a collection must not duplicate a source record."""
    dupes = _scalar(session, """
        SELECT count(*) FROM (
            SELECT annotation_set_id, source_annotation_id
            FROM annotations GROUP BY 1, 2 HAVING count(*) > 1
        ) x
    """)
    assert dupes == 0


def test_source_annotation_id_always_present(session):
    """The link back to the source record is mandatory."""
    assert _scalar(session, "SELECT count(*) FROM annotations WHERE source_annotation_id IS NULL") == 0


def test_gdc_annotations_are_non_spatial(session):
    """GDC notes are administrative; reinterpreting them as image regions would be a fabrication."""
    spatial_gdc = _scalar(session, """
        SELECT count(*) FROM annotations a
        JOIN annotation_sets s ON s.annotation_set_id = a.annotation_set_id
        WHERE s.origin = 'source_provided' AND s.provider LIKE 'GDC%'
          AND (a.scope <> 'case' OR a.target_asset_id IS NOT NULL)
    """)
    assert spatial_gdc == 0


def test_gdc_annotations_preserve_source_entity(session):
    """The GDC entity type and barcode must survive the migration."""
    missing = _scalar(session, """
        SELECT count(*) FROM annotations a
        JOIN annotation_sets s ON s.annotation_set_id = a.annotation_set_id
        WHERE s.provider LIKE 'GDC%'
          AND (a.source_entity_submitter_id IS NULL OR a.source_entity_type IS NULL)
    """)
    assert missing == 0


def test_published_sets_declare_licence_and_citation(session):
    """A published derived collection must carry the terms it was released under."""
    incomplete = _scalar(session, """
        SELECT count(*) FROM annotation_sets
        WHERE origin = 'published_derived'
          AND (license IS NULL OR citation IS NULL OR source_url IS NULL)
    """)
    assert incomplete == 0


def test_algorithmic_results_are_not_marked_reviewed(session):
    """An algorithmic result must never claim pathologist review."""
    bad = _scalar(session, """
        SELECT count(*) FROM annotations a
        JOIN annotation_sets s ON s.annotation_set_id = a.annotation_set_id
        WHERE s.method = 'algorithmic'
          AND a.review_status IN ('pathologist_reviewed', 'reviewed', 'ground_truth')
    """)
    assert bad == 0


def test_rendering_derivatives_reference_their_source(session):
    """A viewer derivative must point back at the original file it was made from."""
    unlinked = _scalar(session, """
        SELECT count(*) FROM data_assets
        WHERE asset_type = 'rendering_cache' AND derived_from_asset_id IS NULL
    """)
    assert unlinked == 0


def test_asset_type_check_constraint_rejects_unknown_types(session):
    """asset_type is validated by CHECK even though it is a varchar."""
    from sqlalchemy.exc import IntegrityError

    try:
        session.execute(text(
            "INSERT INTO data_assets (dataset_id, asset_type, uri) "
            "VALUES ((SELECT min(dataset_id) FROM datasets), 'not_a_real_type', 's3://x/y')"
        ))
        session.flush()
    except IntegrityError:
        session.rollback()
    else:
        session.rollback()
        raise AssertionError("CHECK constraint did not reject an unknown asset_type")


def test_spatial_check_constraint_rejects_untargeted_region(session):
    """The database itself refuses a spatial annotation with no slide."""
    from sqlalchemy.exc import IntegrityError

    try:
        session.execute(text("""
            INSERT INTO annotations (annotation_set_id, source_annotation_id, scope)
            VALUES ((SELECT min(annotation_set_id) FROM annotation_sets), 'test-bad-spatial', 'region')
        """))
        session.flush()
    except IntegrityError:
        session.rollback()
    else:
        session.rollback()
        raise AssertionError("CHECK constraint did not reject a region annotation with no asset")


def test_migration_is_recorded_and_repeatable(engine):
    """Re-running the runner applies nothing and leaves the data untouched."""
    from Database.migrations import run_migrations

    with engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM annotations")).scalar()
        versions = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
    assert ("001",) in [tuple(v) for v in versions]

    applied = run_migrations(engine)
    assert applied == [], f"expected no pending migrations, got {applied}"

    with engine.connect() as conn:
        after = conn.execute(text("SELECT count(*) FROM annotations")).scalar()
    assert after == before
