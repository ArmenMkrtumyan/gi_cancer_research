"""The patient timeline must be honest about timing.

The failure mode these guard against is a plausible-looking timeline built from dates the
source never recorded — a record-keeping timestamp reused as a clinical date, or a missing
visit quietly interpolated.
"""

from sqlalchemy import text

VALID_BASES = {"baseline", "relative_to_diagnosis", "derived_from_specimen", "unknown"}


def _rows(session, sql, **p):
    return session.execute(text(sql), p).fetchall()


def test_timeline_view_exists(session):
    assert session.execute(text("SELECT count(*) FROM case_timeline")).scalar() >= 0


def test_every_event_declares_a_timing_basis(session):
    bases = {r[0] for r in _rows(session, "SELECT DISTINCT timing_basis FROM case_timeline")}
    assert bases, "timeline produced no events at all"
    assert bases <= VALID_BASES, f"unexpected timing_basis values: {bases - VALID_BASES}"


def test_undated_events_are_marked_unknown(session):
    """An event with no day must say so rather than defaulting to a real-looking basis."""
    bad = session.execute(text("""
        SELECT count(*) FROM case_timeline
        WHERE day IS NULL AND timing_basis NOT IN ('unknown')
    """)).scalar()
    assert bad == 0


def test_dated_events_are_never_marked_unknown(session):
    bad = session.execute(text("""
        SELECT count(*) FROM case_timeline WHERE day IS NOT NULL AND timing_basis = 'unknown'
    """)).scalar()
    assert bad == 0


def test_molecular_tests_are_untimed(session):
    """molecular_tests has no clinical date column, so no molecular event may carry a day.

    Its `created_datetime` is a GDC record-keeping timestamp, not the date the test was
    performed, and must never be presented as one.
    """
    dated = session.execute(text(
        "SELECT count(*) FROM case_timeline WHERE event_type = 'molecular_test' AND day IS NOT NULL"
    )).scalar()
    assert dated == 0


def test_diagnosis_baseline_is_day_zero(session):
    """The baseline event defines the axis origin."""
    bad = session.execute(text("""
        SELECT count(*) FROM case_timeline
        WHERE event_type = 'diagnosis' AND (day <> 0 OR timing_basis <> 'baseline')
    """)).scalar()
    assert bad == 0


def test_slide_events_carry_an_asset_and_declare_derived_timing(session):
    """A slide event must link to a viewable asset, and its day is inherited, not recorded."""
    no_asset = session.execute(text(
        "SELECT count(*) FROM case_timeline WHERE event_type = 'slide_available' AND asset_id IS NULL"
    )).scalar()
    assert no_asset == 0

    mislabelled = session.execute(text("""
        SELECT count(*) FROM case_timeline
        WHERE event_type = 'slide_available' AND day IS NOT NULL
          AND timing_basis <> 'derived_from_specimen'
    """)).scalar()
    assert mislabelled == 0


def test_sample_collection_comes_from_procurement_not_biobank_receipt(session):
    """The clinical collection event must read `days_to_sample_procurement`.

    Regression guard. This event was originally built from `days_to_collection`, which the
    GDC dictionary defines as the day the sample was RECEIVED by the Biospecimen Core
    Resource — a shipping/accession date, often years after diagnosis. Presenting it as the
    day tissue was taken from the patient produced timelines showing samples "collected"
    hundreds of days after the patient's recorded death.
    """
    mismatched = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN samples s ON s.sample_id::varchar = t.ref_id
        WHERE t.event_type = 'sample_collection'
          AND t.day IS DISTINCT FROM s.days_to_sample_procurement
    """)).scalar()
    assert mismatched == 0


def test_biobank_receipt_is_its_own_event(session):
    """`days_to_collection` may appear, but only as the administrative event it is."""
    mismatched = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN samples s ON s.sample_id::varchar = t.ref_id
        WHERE t.event_type = 'sample_received'
          AND t.day IS DISTINCT FROM s.days_to_collection
    """)).scalar()
    assert mismatched == 0

    leaked = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN samples s ON s.sample_id::varchar = t.ref_id
        WHERE t.event_type = 'sample_collection'
          AND s.days_to_collection IS NOT NULL
          AND s.days_to_sample_procurement IS DISTINCT FROM s.days_to_collection
          AND t.day = s.days_to_collection
    """)).scalar()
    assert leaked == 0, "a receipt date is being presented as a collection date"


def test_no_sample_is_collected_after_the_patient_died(session):
    """A clinical collection event cannot post-date death.

    This is the symptom that exposed the mislabelling. If a future dataset legitimately
    carries post-mortem procurement (an autopsy specimen), this test SHOULD fail and force a
    human to confirm that reading rather than letting it pass silently.
    """
    impossible = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN cases c ON c.case_id = t.case_id
        WHERE t.event_type = 'sample_collection'
          AND c.days_to_death IS NOT NULL AND t.day > c.days_to_death
    """)).scalar()
    assert impossible == 0


def test_slide_day_is_inherited_from_its_specimen_procurement(session):
    """A slide's day must trace to when its tissue was taken, not when it was shipped."""
    mismatched = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN slides sl ON sl.slide_id::varchar = t.ref_id
        LEFT JOIN samples s ON s.sample_id = sl.sample_id
        WHERE t.event_type = 'slide_available'
          AND t.day IS DISTINCT FROM s.days_to_sample_procurement
    """)).scalar()
    assert mismatched == 0


def test_event_days_match_their_source_records(session):
    """Spot-check that timeline days are copied from the source, not computed."""
    mismatched = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN treatments tr ON tr.treatment_id::varchar = t.ref_id
        WHERE t.event_type = 'treatment_start' AND t.day <> tr.days_to_treatment_start
    """)).scalar()
    assert mismatched == 0

    mismatched_fu = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN follow_ups f ON f.follow_up_id::varchar = t.ref_id
        WHERE t.event_type = 'follow_up' AND t.day <> f.days_to_follow_up
    """)).scalar()
    assert mismatched_fu == 0


def test_timeline_events_belong_to_real_cases(session):
    orphans = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        LEFT JOIN cases c ON c.case_id = t.case_id
        WHERE c.case_id IS NULL
    """)).scalar()
    assert orphans == 0


def test_no_event_count_exceeds_its_source_table(session):
    """Guard against a join fanning one source record into many timeline events."""
    n_events = session.execute(text(
        "SELECT count(*) FROM case_timeline WHERE event_type = 'treatment_start'"
    )).scalar()
    n_source = session.execute(text(
        "SELECT count(*) FROM treatments WHERE days_to_treatment_start IS NOT NULL"
    )).scalar()
    assert n_events == n_source


def test_no_event_depicts_a_treatment_the_patient_did_not_receive(session):
    """`treatments` is largely a modality CHECKLIST, not a course-of-treatment log.

    Most rows answer "did this patient ever receive X?" — 3,063 say `no` against 1,764 `yes`.
    Today no `no` row carries a date, so none reaches the timeline, but nothing in the view
    enforces that: the moment GDC dates one, the timeline would draw a treatment that never
    happened. This asserts the invariant rather than relying on the source's restraint.
    """
    fabricated = session.execute(text("""
        SELECT count(*) FROM case_timeline t
        JOIN treatments tr ON tr.treatment_id::varchar = t.ref_id
        WHERE t.event_type IN ('treatment_start', 'treatment_end')
          AND tr.treatment_or_therapy = 'no'
    """)).scalar()
    assert fabricated == 0


def test_pre_diagnosis_treatments_are_kept_not_dropped(session):
    """Prior treatment history is real data and must stay on the timeline.

    These records sit years before day 0 (down to -8743) because GDC records prior treatment
    against the diagnosis. They are faithful source rows — the UI marks them as history
    rather than hiding them or flagging them as errors.
    """
    n_source = session.execute(text(
        "SELECT count(*) FROM treatments WHERE days_to_treatment_start < 0"
    )).scalar()
    n_events = session.execute(text(
        "SELECT count(*) FROM case_timeline WHERE event_type = 'treatment_start' AND day < 0"
    )).scalar()
    assert n_events == n_source
