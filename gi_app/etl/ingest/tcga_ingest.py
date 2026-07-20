"""Ingest the downloaded TCGA-COAD clinical + biospecimen + annotation TSVs into Postgres.

Applies the schema's documented ETL rules (see docs/schema.dbml + docs/schema_logs.md):
  - resolve case_submitter_id (barcode) -> case_id UUID once; children carry the copy.
  - fold family_histories into cases (0:1 patient attribute).
  - diagnoses has TWO columns named site_of_resection_or_biopsy -> read BY INDEX
    (first = tissue_or_organ_of_origin, second = site_of_resection_or_biopsy).
  - molecular_tests attach to case_id (their raw follow_up parents are empty).
  - follow_ups: load only content-bearing rows (~1479 of 2642).
  - annotations: load only barcode-resolvable rows, resolving case_id from the prefix.
  - de-dup by primary key (e.g. ~25 repeated slide_ids).

Idempotent: clears the per-entity tables for a clean reload each run.
"""

import logging
import os
from datetime import datetime, timezone

import etl_utils as E
from Database.database import SessionLocal
from Database.models import (
    Aliquot,
    Analyte,
    Annotation,
    AnnotationSet,
    Case,
    Diagnosis,
    FollowUp,
    IngestionRun,
    MolecularTest,
    PathologyDetail,
    Portion,
    Sample,
    Slide,
    Treatment,
)
from Database.utils import get_or_create_dataset

logger = logging.getLogger(__name__)

DATASET_NAME = "TCGA-COAD"
DATASET_FOLDER = os.environ.get("TCGA_FOLDER", "TCGA_COAD")
COAD_PAGE = "https://portal.gdc.cancer.gov/projects/TCGA-COAD"

# case_id-scoped child tables, child->parent delete order. cases are cleared by dataset_id
# separately; ingestion_runs is kept (append-only audit log). `annotations` is NOT in this
# list — it is cleared selectively (see reset) so published spatial annotations survive a
# re-ingest of the source project.
_RESET_CASE_SCOPED = [
    "aliquots", "analytes", "portions", "slides",
    "molecular_tests", "follow_ups", "pathology_details", "treatments",
    "samples", "diagnoses",
]


def _p(*parts):
    """Build a path inside the dataset folder under DATA_ROOT.

    Args:
        *parts: Path segments below the dataset folder.

    Returns:
        The joined path.
    """
    return os.path.join(E.DATA_ROOT, DATASET_FOLDER, *parts)


def reset(session, dataset_id):
    """Delete THIS dataset's ingested rows (child->parent order) for a clean reload.

    Scoped by dataset_id so multiple datasets coexist — re-ingesting one project must never
    touch another's data. ingestion_runs is kept (append-only audit log).

    This connector owns exactly one kind of annotation: the `source_provided` GDC set for
    this dataset. Everything else in the annotation system — published spatial collections,
    their representations and their stored files — belongs to a different importer and is
    preserved here. Two consequences:

      * `data_assets` rows are NOT deleted. Spatial annotations point at slide assets by
        `asset_id`, so those ids must stay stable across a re-ingest; `slide_ingest`
        upserts them by URI instead. The `slide_id` link is cleared because the `slides`
        rows themselves are reloaded, and is re-established afterwards.
      * Surviving annotations temporarily lose their `case_id` (the cases they point at are
        about to be deleted and reloaded with the same UUIDs); `relink_published_annotations`
        restores it from the target asset once the new rows are in.

    Args:
        session: The active SQLAlchemy session.
        dataset_id: The dataset whose rows to clear.

    Returns:
        None.
    """
    from sqlalchemy import text
    case_subq = "SELECT case_id FROM cases WHERE dataset_id = :d"

    # Drop this dataset's GDC annotations (and the set itself) — they are re-derived below.
    session.execute(text("""
        DELETE FROM annotation_representations WHERE annotation_id IN (
            SELECT a.annotation_id FROM annotations a
            JOIN annotation_sets s ON s.annotation_set_id = a.annotation_set_id
            WHERE s.dataset_id = :d AND s.origin = 'source_provided')
    """), {"d": dataset_id})
    session.execute(text("""
        DELETE FROM annotations WHERE annotation_set_id IN (
            SELECT annotation_set_id FROM annotation_sets
            WHERE dataset_id = :d AND origin = 'source_provided')
    """), {"d": dataset_id})
    session.execute(text("""
        DELETE FROM annotation_sets WHERE dataset_id = :d AND origin = 'source_provided'
    """), {"d": dataset_id})

    # Detach surviving (published) annotations from the cases about to be deleted.
    session.execute(text(f"""
        UPDATE annotations SET case_id = NULL
        WHERE case_id IN ({case_subq})
    """), {"d": dataset_id})

    # Keep the asset rows; only the slide linkage is rebuilt.
    session.execute(text(
        "UPDATE data_assets SET slide_id = NULL WHERE dataset_id = :d"), {"d": dataset_id})

    for table in _RESET_CASE_SCOPED:
        session.execute(text(f"DELETE FROM {table} WHERE case_id IN ({case_subq})"), {"d": dataset_id})
    session.execute(text("DELETE FROM cases WHERE dataset_id = :d"), {"d": dataset_id})
    session.commit()
    logger.info(f"Cleared existing rows for dataset {dataset_id} (clean reload; "
                "published annotations and stored files preserved).")


def relink_published_annotations(session, dataset_id):
    """Restore `case_id` on preserved spatial annotations after a reload.

    Their target asset is the authority: asset -> slide -> case. Runs after the slides have
    been reloaded and `slide_ingest` has re-linked assets to them.

    Args:
        session: The active SQLAlchemy session.
        dataset_id: The dataset whose annotations to re-link.

    Returns:
        The number of annotations re-linked.
    """
    from sqlalchemy import text
    n = session.execute(text("""
        UPDATE annotations a
        SET case_id = sl.case_id
        FROM data_assets da
        JOIN slides sl ON sl.slide_id = da.slide_id
        WHERE a.target_asset_id = da.asset_id
          AND a.case_id IS NULL
          AND da.dataset_id = :d
    """), {"d": dataset_id}).rowcount
    session.commit()
    if n:
        logger.info(f"Re-linked {n} published annotations to their reloaded cases.")
    return n


# --------------------------------------------------------------------------- #
# Clinical
# --------------------------------------------------------------------------- #
def load_cases(session, dataset_id):
    """Load cases.tsv (family history folded in) into the cases table.

    Args:
        session: The active SQLAlchemy session.
        dataset_id: The dataset these cases belong to.

    Returns:
        A dict mapping case barcode -> case_id, used to resolve every child table.
    """
    fam = {}
    fpath = _p("clinical", "family_histories.tsv")
    if os.path.exists(fpath):
        for r in E.read_dicts(fpath):
            cb = E.s(r.get("case_submitter_id"))
            if cb and cb not in fam:
                fam[cb] = (
                    E.yes_no(r.get("relative_with_cancer_history")),
                    E.to_int(r.get("relatives_with_cancer_history_count")),
                )

    rows, barcode_to_case = [], {}
    for r in E.read_dicts(_p("clinical", "cases.tsv")):
        cid = E.s(r.get("case_id"))
        bc = E.s(r.get("submitter_id"))
        if not cid or not bc:
            continue
        barcode_to_case[bc] = cid
        rel, cnt = fam.get(bc, (None, None))
        rows.append(dict(
            case_id=cid, dataset_id=dataset_id, submitter_id=bc,
            disease_type=E.s(r.get("disease_type")),
            primary_site=E.s(r.get("primary_site")),
            sex_at_birth=E.sex(r.get("demographic.sex_at_birth")),
            race=E.race(r.get("demographic.race")),
            ethnicity=E.ethnicity(r.get("demographic.ethnicity")),
            vital_status=E.vital(r.get("demographic.vital_status")),
            days_to_death=E.to_int(r.get("demographic.days_to_death")),
            lost_to_followup=E.yes_no(r.get("lost_to_followup")),
            country_of_residence_at_enrollment=E.s(r.get("demographic.country_of_residence_at_enrollment")),
            relative_with_cancer_history=rel,
            relatives_with_cancer_history_count=cnt,
        ))
    rows = E.dedup_by_pk(rows, "case_id")
    session.bulk_insert_mappings(Case, rows)
    logger.info(f"cases: {len(rows)}")
    return barcode_to_case


def load_diagnoses(session, cmap):
    """Load diagnoses.tsv into the diagnoses table (reads the duplicate site columns by index).

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.

    Returns:
        The set of loaded diagnosis_ids (used to validate child foreign keys).
    """
    header, raw = E.read_rows(_p("clinical", "diagnoses.tsv"))
    site_idx = [i for i, h in enumerate(header) if h == "site_of_resection_or_biopsy"]
    organ_i = site_idx[0] if site_idx else None
    resect_i = site_idx[1] if len(site_idx) > 1 else None
    idx = {}
    for i, h in enumerate(header):
        idx.setdefault(h, i)  # first occurrence for single-named columns

    def g(row, name):
        """Return the value in `row` for column `name` (first occurrence), or None."""
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    rows, dx_ids = [], set()
    for row in raw:
        did = E.s(g(row, "diagnosis_id"))
        cb = E.s(g(row, "case_submitter_id"))
        cid = cmap.get(cb)
        if not did or not cid:
            continue
        rows.append(dict(
            diagnosis_id=did, case_id=cid, submitter_id=E.s(g(row, "submitter_id")),
            primary_diagnosis=E.s(g(row, "primary_diagnosis")),
            tissue_or_organ_of_origin=E.s(row[organ_i]) if organ_i is not None and organ_i < len(row) else None,
            site_of_resection_or_biopsy=E.s(row[resect_i]) if resect_i is not None and resect_i < len(row) else None,
            ajcc_pathologic_stage=E.s(g(row, "ajcc_pathologic_stage")),
            ajcc_pathologic_t=E.s(g(row, "ajcc_pathologic_t")),
            ajcc_pathologic_n=E.s(g(row, "ajcc_pathologic_n")),
            ajcc_pathologic_m=E.s(g(row, "ajcc_pathologic_m")),
            ajcc_staging_system_edition=E.s(g(row, "ajcc_staging_system_edition")),
            residual_disease=E.s(g(row, "residual_disease")),
            age_at_diagnosis=E.days_to_years(g(row, "age_at_diagnosis")),
            year_of_diagnosis=E.to_int(g(row, "year_of_diagnosis")),
            days_to_last_follow_up=E.to_int(g(row, "days_to_last_follow_up")),
            prior_malignancy=E.yes_no(g(row, "prior_malignancy")),
            prior_treatment=E.yes_no(g(row, "prior_treatment")),
            classification_of_tumor=E.s(g(row, "classification_of_tumor")),
            diagnosis_is_primary_disease=E.to_bool(g(row, "diagnosis_is_primary_disease")),
            synchronous_malignancy=E.yes_no(g(row, "synchronous_malignancy")),
            created_datetime=E.to_dt(g(row, "created_datetime")),
            updated_datetime=E.to_dt(g(row, "updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "diagnosis_id")
    for r in rows:
        dx_ids.add(r["diagnosis_id"])
    session.bulk_insert_mappings(Diagnosis, rows)
    logger.info(f"diagnoses: {len(rows)}")
    return dx_ids


def load_treatments(session, cmap, dx_ids):
    """Load treatments.tsv into the treatments table.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.
        dx_ids: Valid diagnosis_ids (diagnosis_id is kept only if present).

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("clinical", "treatments.tsv")):
        tid = E.s(r.get("treatment_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        if not tid or not cid:
            continue
        did = E.s(r.get("diagnosis_id"))
        rows.append(dict(
            treatment_id=tid, case_id=cid,
            diagnosis_id=did if did in dx_ids else None,
            submitter_id=E.s(r.get("submitter_id")),
            treatment_type=E.s(r.get("treatment_type")),
            treatment_or_therapy=E.yes_no(r.get("treatment_or_therapy")),
            treatment_intent_type=E.s(r.get("treatment_intent_type")),
            therapeutic_agents=E.s(r.get("therapeutic_agents")),
            treatment_outcome=E.s(r.get("treatment_outcome")),
            initial_disease_status=E.s(r.get("initial_disease_status")),
            number_of_cycles=E.to_int(r.get("number_of_cycles")),
            number_of_fractions=E.to_int(r.get("number_of_fractions")),
            treatment_dose=E.to_float(r.get("treatment_dose")),
            treatment_dose_units=E.s(r.get("treatment_dose_units")),
            prescribed_dose=E.to_float(r.get("prescribed_dose")),
            prescribed_dose_units=E.s(r.get("prescribed_dose_units")),
            regimen_or_line_of_therapy=E.s(r.get("regimen_or_line_of_therapy")),
            clinical_trial_indicator=E.yes_no(r.get("clinical_trial_indicator")),
            days_to_treatment_start=E.to_int(r.get("days_to_treatment_start")),
            days_to_treatment_end=E.to_int(r.get("days_to_treatment_end")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "treatment_id")
    session.bulk_insert_mappings(Treatment, rows)
    logger.info(f"treatments: {len(rows)}")


def load_pathology_details(session, cmap, dx_ids):
    """Load pathology_details.tsv into the pathology_details table.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.
        dx_ids: Valid diagnosis_ids (diagnosis_id is kept only if present).

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("clinical", "pathology_details.tsv")):
        pid = E.s(r.get("pathology_detail_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        if not pid or not cid:
            continue
        did = E.s(r.get("diagnosis_id"))
        rows.append(dict(
            pathology_detail_id=pid, case_id=cid,
            diagnosis_id=did if did in dx_ids else None,
            submitter_id=E.s(r.get("submitter_id")),
            lymph_nodes_positive=E.to_int(r.get("lymph_nodes_positive")),
            lymph_nodes_tested=E.to_int(r.get("lymph_nodes_tested")),
            vascular_invasion_present=E.yes_no(r.get("vascular_invasion_present")),
            lymphatic_invasion_present=E.yes_no(r.get("lymphatic_invasion_present")),
            perineural_invasion_present=E.yes_no(r.get("perineural_invasion_present")),
            non_nodal_tumor_deposits=E.yes_no(r.get("non_nodal_tumor_deposits")),
            circumferential_resection_margin=E.to_float(r.get("circumferential_resection_margin")),
            consistent_pathology_review=E.yes_no(r.get("consistent_pathology_review")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "pathology_detail_id")
    session.bulk_insert_mappings(PathologyDetail, rows)
    logger.info(f"pathology_details: {len(rows)}")


_FU_CONTENT = ["timepoint_category", "days_to_follow_up", "disease_response",
               "progression_or_recurrence", "days_to_recurrence", "days_to_progression",
               "progression_or_recurrence_type", "progression_or_recurrence_anatomic_site"]


def load_follow_ups(session, cmap):
    """Load the content-bearing rows of follow_ups.tsv into the follow_ups table.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("clinical", "follow_ups.tsv")):
        fid = E.s(r.get("follow_up_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        if not fid or not cid:
            continue
        if not any(E.s(r.get(c)) for c in _FU_CONTENT):
            continue  # drop contentless / empty molecular-test parent containers
        rows.append(dict(
            follow_up_id=fid, case_id=cid, submitter_id=E.s(r.get("submitter_id")),
            timepoint_category=E.s(r.get("timepoint_category")),
            days_to_follow_up=E.to_int(r.get("days_to_follow_up")),
            disease_response=E.s(r.get("disease_response")),
            progression_or_recurrence=E.yes_no(r.get("progression_or_recurrence")),
            days_to_recurrence=E.to_int(r.get("days_to_recurrence")),
            days_to_progression=E.to_int(r.get("days_to_progression")),
            progression_or_recurrence_type=E.s(r.get("progression_or_recurrence_type")),
            progression_or_recurrence_anatomic_site=E.s(r.get("progression_or_recurrence_anatomic_site")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "follow_up_id")
    session.bulk_insert_mappings(FollowUp, rows)
    logger.info(f"follow_ups: {len(rows)}")


def load_molecular_tests(session, cmap):
    """Load molecular_tests.tsv into the molecular_tests table (attached directly to case_id).

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("clinical", "molecular_tests.tsv")):
        mid = E.s(r.get("molecular_test_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        if not mid or not cid:
            continue
        rows.append(dict(
            molecular_test_id=mid, case_id=cid, submitter_id=E.s(r.get("submitter_id")),
            timepoint_category=E.s(r.get("timepoint_category")),
            molecular_analysis_method=E.s(r.get("molecular_analysis_method")),
            laboratory_test=E.s(r.get("laboratory_test")),
            gene_symbol=E.s(r.get("gene_symbol")),
            antigen=E.s(r.get("antigen")),
            variant_type=E.s(r.get("variant_type")),
            mutation_codon=E.s(r.get("mutation_codon")),
            test_result=E.s(r.get("test_result")),
            test_value=E.to_float(r.get("test_value")),
            test_units=E.s(r.get("test_units")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "molecular_test_id")
    session.bulk_insert_mappings(MolecularTest, rows)
    logger.info(f"molecular_tests: {len(rows)}")


# --------------------------------------------------------------------------- #
# Biospecimen
# --------------------------------------------------------------------------- #
def load_samples(session, cmap):
    """Load samples.tsv into the samples table.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.

    Returns:
        A dict mapping sample barcode -> sample_id, used to resolve biospecimen children.
    """
    rows, smap = [], {}
    for r in E.read_dicts(_p("biospecimen", "samples.tsv")):
        sid = E.s(r.get("sample_id"))
        bc = E.s(r.get("submitter_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        if not sid or not cid:
            continue
        if bc:
            smap[bc] = sid
        rows.append(dict(
            sample_id=sid, case_id=cid, submitter_id=bc,
            sample_type=E.s(r.get("sample_type")),
            tissue_type=E.tissue_type(r.get("tissue_type")),
            specimen_type=E.s(r.get("specimen_type")),
            preservation_method=E.s(r.get("preservation_method")),
            pathology_report_uuid=E.s(r.get("pathology_report_uuid")),
            longest_dimension=E.to_float(r.get("longest_dimension")),
            shortest_dimension=E.to_float(r.get("shortest_dimension")),
            intermediate_dimension=E.to_float(r.get("intermediate_dimension")),
            initial_weight=E.to_float(r.get("initial_weight")),
            days_to_sample_procurement=E.to_int(r.get("days_to_sample_procurement")),
            days_to_collection=E.to_int(r.get("days_to_collection")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "sample_id")
    session.bulk_insert_mappings(Sample, rows)
    logger.info(f"samples: {len(rows)}")
    return smap


def load_portions(session, cmap, smap):
    """Load portions.tsv into the portions table.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.
        smap: Sample barcode -> sample_id lookup.

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("biospecimen", "portions.tsv")):
        pid = E.s(r.get("portion_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        sid = smap.get(E.s(r.get("sample_submitter_id")))
        if not pid or not cid or not sid:
            continue
        rows.append(dict(
            portion_id=pid, case_id=cid, sample_id=sid,
            submitter_id=E.s(r.get("submitter_id")),
            portion_number=E.s(r.get("portion_number")),
            weight=E.to_float(r.get("weight")),
            is_ffpe=E.to_bool(r.get("is_ffpe")),
            creation_datetime=E.to_int(r.get("creation_datetime")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "portion_id")
    session.bulk_insert_mappings(Portion, rows)
    logger.info(f"portions: {len(rows)}")


def load_analytes(session, cmap, smap):
    """Load analytes.tsv into the analytes table.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.
        smap: Sample barcode -> sample_id lookup.

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("biospecimen", "analytes.tsv")):
        aid = E.s(r.get("analyte_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        sid = smap.get(E.s(r.get("sample_submitter_id")))
        if not aid or not cid or not sid:
            continue
        rows.append(dict(
            analyte_id=aid, case_id=cid, sample_id=sid,
            submitter_id=E.s(r.get("submitter_id")),
            analyte_type=E.s(r.get("analyte_type")),
            experimental_protocol_type=E.s(r.get("experimental_protocol_type")),
            concentration=E.to_float(r.get("concentration")),
            spectrophotometer_method=E.s(r.get("spectrophotometer_method")),
            a260_a280_ratio=E.to_float(r.get("a260_a280_ratio")),
            ribosomal_rna_28s_16s_ratio=E.to_float(r.get("ribosomal_rna_28s_16s_ratio")),
            rna_integrity_number=E.to_float(r.get("rna_integrity_number")),
            normal_tumor_genotype_snp_match=E.yes_no(r.get("normal_tumor_genotype_snp_match")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "analyte_id")
    session.bulk_insert_mappings(Analyte, rows)
    logger.info(f"analytes: {len(rows)}")


def load_aliquots(session, cmap, smap):
    """Load aliquots.tsv into the aliquots table.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.
        smap: Sample barcode -> sample_id lookup.

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("biospecimen", "aliquots.tsv")):
        aid = E.s(r.get("aliquot_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        sid = smap.get(E.s(r.get("sample_submitter_id")))
        if not aid or not cid or not sid:
            continue
        rows.append(dict(
            aliquot_id=aid, case_id=cid, sample_id=sid,
            submitter_id=E.s(r.get("submitter_id")),
            aliquot_quantity=E.to_float(r.get("aliquot_quantity")),
            aliquot_volume=E.to_float(r.get("aliquot_volume")),
            concentration=E.to_float(r.get("concentration")),
            source_center=E.s(r.get("source_center")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "aliquot_id")
    session.bulk_insert_mappings(Aliquot, rows)
    logger.info(f"aliquots: {len(rows)}")


def load_slides(session, cmap, smap):
    """Load slides.tsv into the slides table (slide metadata, not the .svs image itself).

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.
        smap: Sample barcode -> sample_id lookup.

    Returns:
        None.
    """
    rows = []
    for r in E.read_dicts(_p("biospecimen", "slides.tsv")):
        sid = E.s(r.get("slide_id"))
        cid = cmap.get(E.s(r.get("case_submitter_id")))
        samp = smap.get(E.s(r.get("sample_submitter_id")))
        bc = E.s(r.get("submitter_id"))
        if not sid or not cid or not samp:
            continue
        rows.append(dict(
            slide_id=sid, case_id=cid, sample_id=samp, submitter_id=bc,
            slide_type=E.slide_type_from_barcode(bc),
            section_location=E.section_location(r.get("section_location")),
            percent_tumor_cells=E.to_float(r.get("percent_tumor_cells")),
            percent_tumor_nuclei=E.to_float(r.get("percent_tumor_nuclei")),
            percent_normal_cells=E.to_float(r.get("percent_normal_cells")),
            percent_stromal_cells=E.to_float(r.get("percent_stromal_cells")),
            percent_necrosis=E.to_float(r.get("percent_necrosis")),
            percent_lymphocyte_infiltration=E.to_float(r.get("percent_lymphocyte_infiltration")),
            percent_neutrophil_infiltration=E.to_float(r.get("percent_neutrophil_infiltration")),
            percent_monocyte_infiltration=E.to_float(r.get("percent_monocyte_infiltration")),
            created_datetime=E.to_dt(r.get("created_datetime")),
            updated_datetime=E.to_dt(r.get("updated_datetime")),
        ))
    rows = E.dedup_by_pk(rows, "slide_id")
    session.bulk_insert_mappings(Slide, rows)
    logger.info(f"slides: {len(rows)}")


# --------------------------------------------------------------------------- #
# Curation
# --------------------------------------------------------------------------- #
def get_or_create_gdc_annotation_set(session, dataset_id, dataset_name):
    """Return the GDC annotation set for this dataset, creating it if absent.

    Collection-level provenance (provider, licence, citation, method) is stored once here
    rather than repeated on all ~1.1k annotation rows.

    Args:
        session: The active SQLAlchemy session.
        dataset_id: The dataset the set belongs to.
        dataset_name: Dataset name, used in the set's display name.

    Returns:
        The existing or newly created AnnotationSet.
    """
    name = f"GDC Annotations — {dataset_name}"
    s = (session.query(AnnotationSet)
         .filter_by(dataset_id=dataset_id, name=name, version="1").one_or_none())
    if s is None:
        s = AnnotationSet(
            dataset_id=dataset_id, name=name,
            provider="GDC (Genomic Data Commons)",
            source_url="https://portal.gdc.cancer.gov/annotations",
            citation=("Genomic Data Commons Data Portal, National Cancer Institute. "
                      "Annotations are administrative/QC notes recorded by the "
                      "data-coordinating centre."),
            license="NCI GDC Data Portal terms of use",
            version="1",
            method="manual",           # curator-entered notes, not model output
            origin="source_provided",
            description=("Administrative and quality-control notes recorded against GDC "
                         "entities. Non-spatial: these carry no slide coordinates, "
                         "polygons or masks."),
            retrieved_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(s)
        session.flush()
    return s


def load_annotations(session, cmap, dataset_id, dataset_name):
    """Load the barcode-resolvable rows of annotations.tsv into the annotations table.

    GDC annotations are administrative/QC notes about an entity — never slide regions — so
    they are recorded as scope='case', annotation_type='qc', with no target asset. The GDC
    annotation UUID is preserved as `source_annotation_id`, and the source entity type and
    barcode are preserved verbatim.

    Args:
        session: The active SQLAlchemy session.
        cmap: Barcode -> case_id lookup.
        dataset_id: The dataset being ingested.
        dataset_name: Dataset name, for the annotation set's display name.

    Returns:
        None.
    """
    path = _p("annotations.tsv")  # annotations.tsv lives at the dataset root
    if not os.path.exists(path):
        logger.warning("annotations.tsv not found — skipping.")
        return
    aset = get_or_create_gdc_annotation_set(session, dataset_id, dataset_name)
    now = datetime.now(timezone.utc)
    rows, skipped = [], 0
    for r in E.read_dicts(path):
        aid = E.s(r.get("annotation_id"))
        ent = E.s(r.get("entity_submitter_id"))
        if not aid or not ent or not ent.startswith("TCGA-"):
            skipped += 1
            continue
        cid = cmap.get(E.case_barcode(ent))
        if not cid:  # genomic-file rows / unknown cases: not barcode-resolvable
            skipped += 1
            continue
        category = E.s(r.get("category"))
        rows.append(dict(
            annotation_set_id=aset.annotation_set_id,
            source_annotation_id=aid,
            case_id=cid,
            target_asset_id=None,      # administrative note: not tied to an image
            scope="case",
            annotation_type="qc",
            label=category,
            category=category,
            classification=E.s(r.get("classification")),
            source_entity_type=E.s(r.get("entity_type")),
            source_entity_submitter_id=ent,
            notes=E.s(r.get("notes")),
            source_created_datetime=E.to_dt(r.get("created_datetime")),
            created_at=now,
        ))
    rows = E.dedup_by_pk(rows, "source_annotation_id")
    session.bulk_insert_mappings(Annotation, rows)
    logger.info(f"annotations: {len(rows)} loaded into set {aset.annotation_set_id}, "
                f"{skipped} skipped (non-barcode/genomic-file)")


def main(dataset_name=DATASET_NAME, project_id="TCGA-COAD", page_url=COAD_PAGE,
         cancer_types="Colon adenocarcinoma", folder=None, access="mixed"):
    """Ingest a TCGA project's clinical + biospecimen + curation TSVs and log the run.

    Args:
        dataset_name: Dataset name (e.g. "TCGA-STAD"); defaults to COAD for CLI back-compat.
        project_id: GDC project id (for logging/parity).
        page_url: Official GDC project page URL.
        cancer_types: Human-readable cancer type(s).
        folder: Source folder under DATA_ROOT (defaults to env TCGA_FOLDER).
        access: Dataset access_type enum value.

    Returns:
        None.
    """
    global DATASET_FOLDER
    if folder:
        DATASET_FOLDER = folder

    session = SessionLocal()
    started = datetime.now(timezone.utc)

    ds = get_or_create_dataset(session, dataset_name, access, page_url, cancer_types)
    session.commit()
    dataset_id = ds.dataset_id

    try:
        reset(session, dataset_id)

        cmap = load_cases(session, dataset_id)
        dx_ids = load_diagnoses(session, cmap)
        load_treatments(session, cmap, dx_ids)
        load_pathology_details(session, cmap, dx_ids)
        load_follow_ups(session, cmap)
        load_molecular_tests(session, cmap)

        smap = load_samples(session, cmap)
        load_portions(session, cmap, smap)
        load_analytes(session, cmap, smap)
        load_aliquots(session, cmap, smap)
        load_slides(session, cmap, smap)

        load_annotations(session, cmap, dataset_id, dataset_name)
        session.commit()
        relink_published_annotations(session, dataset_id)

        session.add(IngestionRun(
            dataset_id=dataset_id, connector="tcga_ingest",
            started_at=started, finished_at=datetime.now(timezone.utc), status="success",
        ))
        session.commit()
        logger.info(f"{dataset_name} ingest complete (dataset_id={dataset_id}).")
    except Exception:
        session.rollback()
        session.add(IngestionRun(
            dataset_id=dataset_id, connector="tcga_ingest", started_at=started,
            finished_at=datetime.now(timezone.utc), status="failed",
        ))
        session.commit()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    from logging_setup import configure_logging
    configure_logging()
    main()
