"""Compatibility report for a registered source link.

Answers the question a user has right after pasting a URL into the "Add data" tool:
*can this actually be loaded, and what will I end up with?* The report names the
source and walks the schema table by table, saying which tables the dataset would
fill and which stay empty.

There are two ways a report gets produced, and they are deliberately different:

* **Connector-derived** — when a connector exists for the source, the report describes
  what that connector actually does, plus live counts queried from the source's own
  API. Nothing is guessed; this is our own code describing itself.
* **Analysed** — for every other link. There is no way to enumerate in advance the
  archives a researcher might paste, so anything without a connector has to be read
  and assessed at request time. That path lives in `analysis.py`; this module only
  provides the shared vocabulary (TABLES, verdicts) and the routing.

Read-only and side-effect free — safe to call from a request handler.
"""

# How completely a source can populate one table.
FULL = "full"        # the loader fills this the same way TCGA does
PARTIAL = "partial"  # some columns land, but expect gaps or free-text values
NONE = "none"        # nothing to put here

# Overall verdicts, ordered worst-to-best in terms of what the user must do next.
UNKNOWN = "unknown"            # nothing is known about this link yet
UNSUPPORTED = "unsupported"    # cannot be automated (approvals, agreements, paywall)
NEEDS_REVIEW = "needs_review"  # real mismatch to resolve before building
PARTIAL_FIT = "partial"        # would fit well, connector not built yet
SUPPORTED = "supported"        # connector exists, download is enabled

# Where a report came from, so the UI can be honest about its confidence.
BY_CONNECTOR = "connector"  # derived from a connector we wrote; exact
BY_ANALYSIS = "analysis"    # read and assessed at request time; best effort
BY_NOTHING = "none"         # not assessed

# The schema chain, in the order a user reads it (patient -> tissue -> image).
# The biospecimen sub-chain is collapsed into one row because those three tables are
# always filled together from the same source file; splitting them adds no signal.
TABLES = (
    ("cases", "Patients", "One row per patient: demographics, vital status."),
    ("diagnoses", "Diagnoses", "Stage, morphology, primary site."),
    ("treatments", "Treatments", "Therapies recorded for each diagnosis."),
    ("pathology_details", "Pathology details", "Measurements from the pathology report."),
    ("follow_ups", "Follow-ups", "Repeat visits and survival updates."),
    ("molecular_tests", "Molecular tests", "Biomarker and subtype results."),
    ("samples", "Samples", "Tissue samples taken from each patient."),
    ("biospecimen", "Portions / analytes / aliquots", "The biospecimen chain below each sample."),
    ("slides", "Slides", "Slide records linked to a sample."),
    ("data_assets", "Data assets", "The stored files themselves (slide images, etc.)."),
    ("annotations", "Annotations", "QC flags and labels attached to cases or slides."),
)

TABLE_KEYS = tuple(key for key, _, _ in TABLES)


def empty_fills():
    """Every table defaulting to NONE, ready to be overridden."""
    return {key: (NONE, None) for key in TABLE_KEYS}


def finalise(report, fills):
    """Attach the ordered, UI-facing table list and the filled/total counts.

    Args:
        report: The report dict being built.
        fills: {table_key: (fill_level, note)} for every key in TABLE_KEYS.

    Returns:
        The same report, with `tables`, `n_tables_filled` and `n_tables_total` set.
    """
    report["tables"] = [
        {
            "table": key,
            "label": label,
            "description": description,
            "fill": fills[key][0],
            "note": fills[key][1],
        }
        for key, label, description in TABLES
    ]
    report["n_tables_filled"] = len([t for t in report["tables"] if t["fill"] != NONE])
    report["n_tables_total"] = len(report["tables"])
    return report


def _base(source_url, source_type, source_label, analysed_by):
    """The fields every report carries, whichever path produced it."""
    return {
        "source_url": source_url,
        "source_type": source_type,
        "source_label": source_label,
        "analysed_by": analysed_by,
        "connector": None,
        "downloadable": False,
        "verdict": UNKNOWN,
        "headline": "",
        "accession": None,
        # Every path states how far its own report can be trusted, so the UI never has
        # to infer it from `analysed_by`.
        "confidence": "low",
        "confidence_reason": "",
        "probe": None,
        "probe_error": None,
        "citations": [],
        "warnings": [],
        "next_steps": [],
    }


# --------------------------- connector-derived ----------------------------- #
def _probe_gdc(url):
    """Live counts for a GDC project: patients and open tumour slides by type.

    Args:
        url: The registered source link (a GDC project page).

    Returns:
        A dict of facts, or {"error": str} if the project could not be resolved or
        queried. Never raises.
    """
    try:
        import gdc_acquire  # on the ETL path; imported here so `shared` stays standalone
    except ImportError:
        return {"error": "GDC connector is not importable from this process."}

    try:
        project = gdc_acquire.resolve_project(url)
    except Exception as exc:
        return {"error": str(exc)}

    try:
        diagnostic = gdc_acquire.query_slides(project, "Diagnostic Slide")
        tissue = gdc_acquire.query_slides(project, "Tissue Slide")
        n_cases = gdc_acquire.count_cases(project)
    except Exception as exc:
        return {"error": f"GDC query failed: {exc}"}

    slides = diagnostic + tissue
    return {
        "accession": project,
        "n_cases": n_cases,
        "n_slides": len(slides),
        "n_diagnostic_slides": len(diagnostic),
        "n_tissue_slides": len(tissue),
        "total_mb": round(sum(h["_size"] for h in slides) / 1e6, 1),
        "has_images": bool(slides),
    }


def gdc_report(source_url, live=True):
    """What the TCGA/GDC connector would load. Describes our own loaders, not a guess.

    Every table is marked FULL because `tcga_ingest` + `slide_ingest` populate the whole
    chain — if that stops being true, this function is what needs updating.
    """
    report = _base(source_url, "gdc", "TCGA / GDC", BY_CONNECTOR)
    report.update({
        "connector": "gdc_acquire + tcga_ingest",
        "downloadable": True,
        "verdict": SUPPORTED,
        "headline": "Fully supported. This is the source the schema was built around.",
        "confidence": "high",
        "confidence_reason": "Describes what the TCGA loaders do, with counts queried "
                             "live from the GDC API. Nothing here is inferred.",
        "warnings": [
            "Only open-access files are pulled. Raw sequencing (BAM/FASTQ) and germline "
            "data sit behind dbGaP approval and are skipped.",
            "Slides are sampled by default — pick 'All slides' for the complete set.",
        ],
    })

    fills = {key: (FULL, None) for key in TABLE_KEYS}

    if live:
        probe = _probe_gdc(source_url)
        if probe.get("error"):
            report["probe_error"] = probe["error"]
            report["warnings"].append(
                f"Could not read live details from GDC: {probe['error']}")
        else:
            report["probe"] = probe
            report["accession"] = probe["accession"]
            if not probe["has_images"]:
                report["warnings"].append(
                    "This project has no open-access tumour slides, so the slide tables "
                    "stay empty.")
                fills["slides"] = (NONE, "No open slides available for this project.")
                fills["data_assets"] = (NONE, "Nothing to store without slides.")

    return finalise(report, fills)


CONNECTOR_REPORTS = {"gdc": gdc_report}


# ------------------------------- routing ----------------------------------- #
def unanalysed_report(source_url, source_type, reason):
    """A placeholder for links nothing has looked at yet.

    Deliberately says nothing about which tables would fill. Claiming "0 of 11" for an
    unread link reads as a verdict when it is really an absence of one.
    """
    report = _base(source_url, source_type, "Not yet analysed", BY_NOTHING)
    report.update({
        "verdict": UNKNOWN,
        "headline": "This link has not been analysed yet.",
        "confidence_reason": "No assessment was produced, so the table breakdown below "
                             "reflects an absence of information, not a finding.",
        "warnings": [reason],
        "next_steps": ["Run an analysis on this link to find out what it contains."],
    })
    return finalise(report, empty_fills())


def build_report(source_url, source_type=None, live=True):
    """Describe what would happen if this source were ingested.

    Routes to the connector-derived report when one exists, and otherwise to the
    analysis path. Sources with no connector and no working analysis come back as
    explicitly un-analysed rather than as a fabricated verdict.

    Args:
        source_url: The registered link.
        source_type: Detected type; re-detected from the URL when omitted.
        live: Whether to reach out to the network at all. False keeps the report
            offline and instant, which also means no analysis.

    Returns:
        A report dict: verdict, headline, per-table fill levels, warnings, next steps,
        and how the report was produced (`analysed_by`).
    """
    from Database.utils import detect_source_type

    source_type = source_type or detect_source_type(source_url)

    if source_type in CONNECTOR_REPORTS:
        return CONNECTOR_REPORTS[source_type](source_url, live=live)

    if not live:
        return unanalysed_report(
            source_url, source_type,
            "Offline report requested, so this link was not read.")

    # Imported here rather than at module scope: `compat` must stay importable (and the
    # connector path must keep working) on a box with no Gemini SDK or no API key.
    import analysis

    try:
        return analysis.analyse_source(source_url, source_type=source_type)
    except analysis.AnalysisError as exc:
        return unanalysed_report(source_url, source_type, str(exc))
