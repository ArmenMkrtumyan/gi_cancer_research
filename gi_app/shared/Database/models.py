"""SQLAlchemy models — the 15-table Phase-1 schema.

Source of truth: docs/schema.dbml (+ docs/schema_logs.md for column rationale and ETL
edge cases). Keep this file in lockstep with the DBML.

Typing policy (from the DBML): only CLOSED controlled vocabularies are Postgres enums
(defined below); evolving clinical vocabularies (stage/TNM/edition/treatment/etc.) are
String, validated/normalized in ETL. Survival is a VIEW (see init_db.py), not a table.
`case_id` on portions/analytes/aliquots/slides is a deliberate controlled denormalization
(ETL invariant: child.case_id == its sample's case_id).
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from Database.database import Base

# ---------------------------------------------------------------------------
# Enums — ONLY closed/stable vocabularies (mirrors schema.dbml). Reuse the same
# type object across columns so each Postgres enum type is created exactly once.
# ---------------------------------------------------------------------------
dataset_access_type = Enum("public", "controlled", "mixed", "private", name="dataset_access_type")
ingest_connector = Enum("tcga_ingest", "geo_ingest", name="ingest_connector")
run_status = Enum("success", "failed", "partial", name="run_status")
sex_at_birth = Enum("male", "female", "unknown", name="sex_at_birth")
vital_status = Enum("alive", "dead", "not_reported", name="vital_status")
race = Enum(
    "white",
    "black_or_african_american",
    "asian",
    "american_indian_or_alaska_native",
    "native_hawaiian_or_other_pacific_islander",
    "not_reported",
    name="race",
)
ethnicity = Enum("hispanic_or_latino", "not_hispanic_or_latino", "not_reported", name="ethnicity")
yes_no = Enum("yes", "no", "not_reported", name="yes_no")
tissue_type = Enum("tumor", "normal", name="tissue_type")
section_location = Enum("top", "bottom", "not_reported", name="section_location")
asset_type = Enum("wsi", name="asset_type")
asset_layer = Enum("bronze", "silver", "gold", name="asset_layer")


# ---------------------------------------------------------------------------
# Catalog & provenance
# ---------------------------------------------------------------------------
class Dataset(Base):
    """A source dataset in the catalog (e.g. TCGA-COAD or a GEO series)."""

    __tablename__ = "datasets"

    dataset_id = Column(Integer, primary_key=True, autoincrement=False)  # our surrogate id
    name = Column(String, nullable=False)  # TCGA-COAD, GSE39582, ...
    access_type = Column(dataset_access_type, nullable=False)
    official_page = Column(String)  # source URL; also identifies the provider
    gi_cancer_types = Column(String)  # e.g. Colon adenocarcinoma


class IngestionRun(Base):
    """Audit log: one row per ETL run for a dataset."""

    __tablename__ = "ingestion_runs"

    run_id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, ForeignKey("datasets.dataset_id"), nullable=False, index=True)
    connector = Column(ingest_connector, nullable=False)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    status = Column(run_status, nullable=False)
    log_uri = Column(String)


# ---------------------------------------------------------------------------
# Clinical (per case)
# ---------------------------------------------------------------------------
class Case(Base):
    """A patient. The centre of the schema — most tables link back here."""

    __tablename__ = "cases"
    __table_args__ = (UniqueConstraint("dataset_id", "submitter_id", name="uq_cases_dataset_submitter"),)

    case_id = Column(UUID(as_uuid=False), primary_key=True)  # GDC case UUID
    dataset_id = Column(Integer, ForeignKey("datasets.dataset_id"), nullable=False, index=True)
    submitter_id = Column(String, nullable=False)  # TCGA barcode, e.g. TCGA-AA-3562
    disease_type = Column(String)
    primary_site = Column(String)
    sex_at_birth = Column(sex_at_birth)
    race = Column(race)
    ethnicity = Column(ethnicity)
    vital_status = Column(vital_status)
    days_to_death = Column(Integer)
    lost_to_followup = Column(yes_no)
    country_of_residence_at_enrollment = Column(String)
    # family history folded in (0:1 patient-level attribute)
    relative_with_cancer_history = Column(yes_no)
    relatives_with_cancer_history_count = Column(Integer)


class Diagnosis(Base):
    """A cancer diagnosis for a case; holds staging/TNM."""

    __tablename__ = "diagnoses"

    diagnosis_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    submitter_id = Column(String)
    primary_diagnosis = Column(String)
    tissue_or_organ_of_origin = Column(String)  # raw diagnoses.tsv column INDEX 4
    site_of_resection_or_biopsy = Column(String)  # raw diagnoses.tsv column INDEX 10
    ajcc_pathologic_stage = Column(String)
    ajcc_pathologic_t = Column(String)
    ajcc_pathologic_n = Column(String)
    ajcc_pathologic_m = Column(String)
    ajcc_staging_system_edition = Column(String)
    residual_disease = Column(String)
    age_at_diagnosis = Column(Integer)  # YEARS (ETL converts from raw days)
    year_of_diagnosis = Column(Integer)
    days_to_last_follow_up = Column(Integer)
    prior_malignancy = Column(yes_no)
    prior_treatment = Column(yes_no)
    classification_of_tumor = Column(String)
    diagnosis_is_primary_disease = Column(Boolean)
    synchronous_malignancy = Column(yes_no)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class Treatment(Base):
    """A treatment given for a diagnosis (chemo, radiation, surgery, ...)."""

    __tablename__ = "treatments"

    treatment_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    diagnosis_id = Column(UUID(as_uuid=False), ForeignKey("diagnoses.diagnosis_id"), index=True)
    submitter_id = Column(String)
    treatment_type = Column(String)
    treatment_or_therapy = Column(yes_no)
    treatment_intent_type = Column(String)
    therapeutic_agents = Column(String)
    treatment_outcome = Column(String)
    initial_disease_status = Column(String)
    number_of_cycles = Column(Integer)
    number_of_fractions = Column(Integer)
    treatment_dose = Column(Float)
    treatment_dose_units = Column(String)
    prescribed_dose = Column(Float)
    prescribed_dose_units = Column(String)
    regimen_or_line_of_therapy = Column(String)
    clinical_trial_indicator = Column(yes_no)
    days_to_treatment_start = Column(Integer)
    days_to_treatment_end = Column(Integer)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class PathologyDetail(Base):
    """Pathologist's specimen findings for a diagnosis (node counts, invasion markers)."""

    __tablename__ = "pathology_details"

    pathology_detail_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    diagnosis_id = Column(UUID(as_uuid=False), ForeignKey("diagnoses.diagnosis_id"), index=True)
    submitter_id = Column(String)
    lymph_nodes_positive = Column(Integer)
    lymph_nodes_tested = Column(Integer)
    vascular_invasion_present = Column(yes_no)
    lymphatic_invasion_present = Column(yes_no)
    perineural_invasion_present = Column(yes_no)
    non_nodal_tumor_deposits = Column(yes_no)
    circumferential_resection_margin = Column(Float)
    consistent_pathology_review = Column(yes_no)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class FollowUp(Base):
    """A follow-up timepoint for a case; feeds survival."""

    __tablename__ = "follow_ups"

    follow_up_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    submitter_id = Column(String)
    timepoint_category = Column(String)
    days_to_follow_up = Column(Integer)
    disease_response = Column(String)
    progression_or_recurrence = Column(yes_no)
    days_to_recurrence = Column(Integer)
    days_to_progression = Column(Integer)
    progression_or_recurrence_type = Column(String)
    progression_or_recurrence_anatomic_site = Column(String)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class MolecularTest(Base):
    """A clinical molecular/biomarker test result (MMR, MSI, KRAS, CEA)."""

    __tablename__ = "molecular_tests"

    molecular_test_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    submitter_id = Column(String)
    timepoint_category = Column(String)
    molecular_analysis_method = Column(String)
    laboratory_test = Column(String)
    gene_symbol = Column(String)
    antigen = Column(String)
    variant_type = Column(String)
    mutation_codon = Column(String)
    test_result = Column(String)
    test_value = Column(Float)
    test_units = Column(String)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Biospecimen (per sample). `case_id` is a controlled denormalization on the
# children (portions/analytes/aliquots/slides).
# ---------------------------------------------------------------------------
class Sample(Base):
    """A physical specimen taken from a patient."""

    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("submitter_id", name="uq_samples_submitter"),)

    sample_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    submitter_id = Column(String, nullable=False)  # sample barcode, e.g. TCGA-AA-3562-01A
    sample_type = Column(String)
    tissue_type = Column(tissue_type)
    specimen_type = Column(String)
    preservation_method = Column(String)
    pathology_report_uuid = Column(String)
    longest_dimension = Column(Float)
    shortest_dimension = Column(Float)
    intermediate_dimension = Column(Float)
    initial_weight = Column(Float)
    days_to_collection = Column(Integer)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class Portion(Base):
    """A piece cut from a sample (part of the molecular-prep tree)."""

    __tablename__ = "portions"

    portion_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    sample_id = Column(UUID(as_uuid=False), ForeignKey("samples.sample_id"), nullable=False, index=True)
    submitter_id = Column(String)
    portion_number = Column(String)
    weight = Column(Float)
    is_ffpe = Column(Boolean)
    creation_datetime = Column(Integer)  # real lab creation date as Unix epoch
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class Analyte(Base):
    """Extracted DNA/RNA from a portion, with quality metrics."""

    __tablename__ = "analytes"

    analyte_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    sample_id = Column(UUID(as_uuid=False), ForeignKey("samples.sample_id"), nullable=False, index=True)
    submitter_id = Column(String)
    analyte_type = Column(String)
    experimental_protocol_type = Column(String)
    concentration = Column(Float)
    spectrophotometer_method = Column(String)
    a260_a280_ratio = Column(Float)
    ribosomal_rna_28s_16s_ratio = Column(Float)
    rna_integrity_number = Column(Float)
    normal_tumor_genotype_snp_match = Column(yes_no)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class Aliquot(Base):
    """A measured sub-portion of an analyte prepared for an assay."""

    __tablename__ = "aliquots"

    aliquot_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    sample_id = Column(UUID(as_uuid=False), ForeignKey("samples.sample_id"), nullable=False, index=True)
    submitter_id = Column(String)
    aliquot_quantity = Column(Float)
    aliquot_volume = Column(Float)
    concentration = Column(Float)
    source_center = Column(String)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


class Slide(Base):
    """Pathologist metadata for a slide (tumor %, section); the .svs image is a DataAsset."""

    __tablename__ = "slides"
    __table_args__ = (UniqueConstraint("submitter_id", name="uq_slides_submitter"),)

    slide_id = Column(UUID(as_uuid=False), primary_key=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), nullable=False, index=True)
    sample_id = Column(UUID(as_uuid=False), ForeignKey("samples.sample_id"), nullable=False, index=True)
    submitter_id = Column(String, nullable=False)  # slide barcode; joins to the .svs in data_assets
    slide_type = Column(String)  # DX/TS/BS/MS, derived from barcode suffix
    section_location = Column(section_location)
    percent_tumor_cells = Column(Float)
    percent_tumor_nuclei = Column(Float)
    percent_normal_cells = Column(Float)
    percent_stromal_cells = Column(Float)
    percent_necrosis = Column(Float)
    percent_lymphocyte_infiltration = Column(Float)
    percent_neutrophil_infiltration = Column(Float)
    percent_monocyte_infiltration = Column(Float)
    created_datetime = Column(DateTime(timezone=True))
    updated_datetime = Column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------
class Annotation(Base):
    """A GDC QC/administrative flag, resolved to a case."""

    __tablename__ = "annotations"

    annotation_id = Column(UUID(as_uuid=False), primary_key=True)
    entity_type = Column(String)
    entity_submitter_id = Column(String, nullable=False, index=True)
    case_id = Column(UUID(as_uuid=False), ForeignKey("cases.case_id"), index=True)
    category = Column(String)
    classification = Column(String)
    notes = Column(Text)
    created_datetime = Column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# File pointers → object storage
# ---------------------------------------------------------------------------
class DataAsset(Base):
    """Pointer to a file in object storage (e.g. a .svs slide in MinIO/S3)."""

    __tablename__ = "data_assets"

    asset_id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, ForeignKey("datasets.dataset_id"), nullable=False, index=True)
    slide_id = Column(UUID(as_uuid=False), ForeignKey("slides.slide_id"), index=True)  # set for wsi
    asset_type = Column(asset_type)
    layer = Column(asset_layer)
    uri = Column(String, nullable=False)  # s3://gi-cancer/bronze/TCGA-COAD/slides/<file>.svs
    format = Column(String)  # svs, parquet, ...
    md5 = Column(String)
    size_bytes = Column(BigInteger)
    source_file_id = Column(String)  # GDC file UUID
    created_at = Column(DateTime(timezone=True))
