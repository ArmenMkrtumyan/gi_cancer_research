#!/usr/bin/env python3
"""
Catalog-driven GDC acquisition tool.

Reads a row from Data.xlsx (sheet "Dataset Matrix"), resolves the GDC project,
and SELECTS a reproducible sample per the Phase-1 spec:
  - clinical + biospecimen (TSV)
  - 3 diagnostic slides: 2 with a GDC annotation, 1 without
  - 3 tissue slides:     2 with a GDC annotation, 1 without
with a size spread (smallest / middle / largest) across each slide triple.

Two-phase by design (matches the agreed workflow):
  1) `--plan`     -> query GDC, choose files, write manifest.txt + a readable
                    report. Downloads NOTHING. You review.
  2) `--download` -> after you approve, fetch the selected files (md5-verified)
                    + clinical/biospecimen TSVs into Data/<project>/.

NOTE on "annotations": a GDC annotation is an administrative/quality note on a
case/sample/slide (e.g. "item flagged", "prior malignancy") -- NOT a
pathologist-drawn tumor-region mask. We sample a mix only to exercise the
annotation-tracker feature, not as training labels.

Examples:
    python gdc_acquire.py --xlsx ../../../Data/Data.xlsx --row 1 --plan
    python gdc_acquire.py --xlsx ../../../Data/Data.xlsx --row 1 --download
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse

GDC_FILES = "https://api.gdc.cancer.gov/files"
GDC_DATA = "https://api.gdc.cancer.gov/data"
GDC_ANN = "https://api.gdc.cancer.gov/annotations"
GDC_CASES = "https://api.gdc.cancer.gov/cases"
SHEET = "Dataset Matrix"


class AcquireError(Exception):
    """Raised when acquisition can't proceed (non-GDC URL, no slides, etc.)."""


# ----------------------------- Data.xlsx ----------------------------------- #
def read_catalog_row(xlsx_path, row_1based):
    """Return {name, page} for the Nth data row of the Dataset Matrix, skipping blank rows."""
    import openpyxl  # only the CLI xlsx path needs it (kept out of the importable library API)
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[SHEET]
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    col = {name: i for i, name in enumerate(header) if name}
    name_i = col["Dataset name"]
    page_i = col["Official dataset page"]
    data_rows = [r for r in rows[1:] if r[name_i]]  # skip blanks
    if row_1based < 1 or row_1based > len(data_rows):
        sys.exit(f"--row {row_1based} out of range (1..{len(data_rows)})")
    r = data_rows[row_1based - 1]
    return {"name": r[name_i], "page": r[page_i]}


def resolve_project(page_url):
    """Parse a GDC project_id from the Official dataset page URL."""
    if not page_url or "gdc.cancer.gov" not in page_url:
        raise AcquireError(f"Not a GDC/TCGA dataset (page={page_url!r}); GEO connector handles those.")
    # .../projects/TCGA-COAD  ->  TCGA-COAD
    return page_url.rstrip("/").split("/")[-1]


# ----------------------------- GDC queries --------------------------------- #
def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def query_slides(project, strategy):
    payload = {
        "filters": {"op": "and", "content": [
            {"op": "in", "content": {"field": "cases.project.project_id", "value": [project]}},
            {"op": "in", "content": {"field": "data_type", "value": ["Slide Image"]}},
            {"op": "in", "content": {"field": "experimental_strategy", "value": [strategy]}},
            {"op": "in", "content": {"field": "access", "value": ["open"]}},
            {"op": "in", "content": {"field": "cases.samples.tissue_type", "value": ["tumor"]}},
        ]},
        "fields": "file_id,file_name,file_size,md5sum,annotations.annotation_id,cases.submitter_id",
        "format": "JSON", "size": "5000",
    }
    hits = _post(GDC_FILES, payload)["data"]["hits"]
    stype = "diagnostic" if "Diagnostic" in strategy else "tissue"
    for h in hits:
        h["_ann"] = len(h.get("annotations") or [])
        h["_size"] = h.get("file_size", 0)
        h["_barcode"] = h["file_name"].split(".")[0]
        h["_case"] = (h.get("cases") or [{}])[0].get("submitter_id", "?")
        h["_type"] = stype
    return hits


def select_spread(hits, n):
    """Pick up to `n` slides spread across the size range (smallest → largest)."""
    hits = sorted(hits, key=lambda h: h["_size"])
    if n <= 0 or not hits:
        return []
    if n >= len(hits):
        return hits
    if n == 1:
        return [hits[len(hits) // 2]]
    step = (len(hits) - 1) / (n - 1)
    idxs = sorted({round(i * step) for i in range(n)})
    return [hits[i] for i in idxs]


# ----------------------------- outputs ------------------------------------- #
def write_manifest(selected, path):
    """gdc-client compatible manifest (minimal)."""
    lines = ["id\tfilename\tmd5\tsize\tstate"]
    for h in selected:
        lines.append(f"{h['file_id']}\t{h['file_name']}\t{h.get('md5sum','')}\t{h['_size']}\treleased")
    open(path, "w").write("\n".join(lines) + "\n")


def write_review_manifest(selected, path):
    """Human-readable slide manifest (TSV) from a flat selected list. Annotation data is
    intentionally NOT here — it lives in annotations.tsv (its own table), joined by `case`."""
    cols = ["slide_type", "size_mb", "case", "barcode", "file_name", "file_id", "md5"]
    lines = ["\t".join(cols)]
    for h in selected:
        lines.append("\t".join([
            h["_type"], f"{h['_size']/1e6:.1f}", h["_case"], h["_barcode"],
            h["file_name"], h["file_id"], h.get("md5sum", ""),
        ]))
    open(path, "w").write("\n".join(lines) + "\n")


def print_report(project, selected, review_path):
    total = sum(h["_size"] for h in selected)
    print(f"\n{'='*80}\n  PLAN — {project}: {len(selected)} slides, {total/1e9:.2f} GB\n{'='*80}")
    for h in selected:
        print(f"    {h['_type']:11} {h['_size']/1e6:7.1f}MB  {h['_case']:14} {h['file_name'][:44]}")
    print(f"  + normalized clinical/ + biospecimen/ + annotations.tsv (fetched on download)")
    print(f"  manifest:  {review_path}")
    print(f"\n  Review the files above. To download, re-run with --download\n")


# ----------------------------- download ------------------------------------ #
def download_selected(selected, outdir, on_progress=None):
    """Download the selected slides (md5-verified).

    Args:
        selected: The chosen slide hits.
        outdir: Destination directory.
        on_progress: Optional callback(slide_index, total_slides, hit, file_done, file_size)
            reporting progress for the CURRENT file (hit is that slide's dict; file_done /
            file_size are its own bytes, not an aggregate). Fired at each file's start
            (file_done=0), throttled while it streams, and on completion — so callers can
            show exactly which file is downloading and how far along it is.
    """
    import hashlib
    os.makedirs(outdir, exist_ok=True)
    total = len(selected)
    for i, h in enumerate(selected, 1):
        file_size = h["_size"]
        dest = os.path.join(outdir, h["file_name"])
        if os.path.exists(dest) and os.path.getsize(dest) == file_size:
            print(f"  [{i}/{total}] exists, skipping {h['file_name']}")
            if on_progress:
                on_progress(i, total, h, file_size, file_size)
            continue
        print(f"  [{i}/{total}] {h['file_name']} ({file_size/1e6:.0f} MB)...", flush=True)
        if on_progress:
            on_progress(i, total, h, 0, file_size)  # announce this file at 0%
        m = hashlib.md5()
        file_done = 0
        since = 0
        with urllib.request.urlopen(f"{GDC_DATA}/{h['file_id']}", timeout=300) as r, open(dest, "wb") as o:
            while True:
                c = r.read(1 << 20)
                if not c:
                    break
                o.write(c); m.update(c)
                file_done += len(c); since += len(c)
                if on_progress and since >= (64 << 20):  # report every ~64 MB
                    on_progress(i, total, h, file_done, file_size); since = 0
        if h.get("md5sum") and m.hexdigest() != h["md5sum"]:
            print(f"      !! md5 MISMATCH for {h['file_name']}", file=sys.stderr)
        if on_progress:
            on_progress(i, total, h, file_size, file_size)  # this file complete


def fetch_annotations(project, outdir):
    """Pull all GDC annotations for the project as TSV. These are
    administrative/clinical-history notes (e.g. 'Prior malignancy'),
    not pathology region masks; stored as case-level metadata."""
    base = "https://api.gdc.cancer.gov/annotations"
    filt = json.dumps({"op": "in", "content": {"field": "project.project_id", "value": [project]}})
    fields = ("annotation_id,entity_type,entity_submitter_id,category,"
              "classification,notes,status,created_datetime")
    os.makedirs(outdir, exist_ok=True)
    params = urllib.parse.urlencode({"filters": filt, "fields": fields, "format": "TSV", "size": "20000"})
    with urllib.request.urlopen(f"{base}?{params}", timeout=120) as r:
        open(os.path.join(outdir, "annotations.tsv"), "wb").write(r.read())
    print("  wrote annotations.tsv")


def _write_tsv(rows, path):
    """Write a list of dicts to a TSV using the union of keys (stable order)."""
    if not rows:
        return
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + "\n")


def fetch_clinical_biospecimen(project, outdir):
    """Pull the COMPLETE clinical + biospecimen tree from the cases endpoint and
    write NORMALIZED one-row-per-entity TSVs (mirrors the portal multi-file shape):
      clinical/    cases, diagnoses, treatments, follow_ups, exposures, family_histories
      biospecimen/ samples, portions, slides, analytes, aliquots
    """
    from collections import defaultdict
    expand = ",".join([
        "demographic", "diagnoses", "diagnoses.treatments", "diagnoses.pathology_details",
        "follow_ups", "follow_ups.molecular_tests", "exposures", "family_histories",
        "samples", "samples.portions", "samples.portions.slides",
        "samples.portions.analytes", "samples.portions.analytes.aliquots",
    ])
    filt = json.dumps({"op": "in", "content": {"field": "project.project_id", "value": [project]}})
    params = urllib.parse.urlencode({"filters": filt, "expand": expand, "format": "JSON", "size": "5000"})
    with urllib.request.urlopen(f"https://api.gdc.cancer.gov/cases?{params}", timeout=180) as r:
        cases = json.loads(r.read())["data"]["hits"]

    def scalars(d, **extra):
        row = dict(extra)
        for k, v in d.items():
            if not isinstance(v, (list, dict)):
                row[k] = v
        return row

    t = defaultdict(list)
    for c in cases:
        cid = c.get("submitter_id")
        crow = scalars(c)
        for k, v in (c.get("demographic") or {}).items():
            if not isinstance(v, (list, dict)):
                crow[f"demographic.{k}"] = v
        t["clinical/cases"].append(crow)
        for dg in c.get("diagnoses") or []:
            did = dg.get("diagnosis_id")
            t["clinical/diagnoses"].append(scalars(dg, case_submitter_id=cid))
            for tr in dg.get("treatments") or []:
                t["clinical/treatments"].append(scalars(tr, case_submitter_id=cid, diagnosis_id=did))
            for pd in dg.get("pathology_details") or []:
                t["clinical/pathology_details"].append(scalars(pd, case_submitter_id=cid, diagnosis_id=did))
        for fu in c.get("follow_ups") or []:
            t["clinical/follow_ups"].append(scalars(fu, case_submitter_id=cid))
            for mt in fu.get("molecular_tests") or []:
                t["clinical/molecular_tests"].append(scalars(mt, case_submitter_id=cid, follow_up_id=fu.get("follow_up_id")))
        for ex in c.get("exposures") or []:
            t["clinical/exposures"].append(scalars(ex, case_submitter_id=cid))
        for fh in c.get("family_histories") or []:
            t["clinical/family_histories"].append(scalars(fh, case_submitter_id=cid))
        for s in c.get("samples") or []:
            sid = s.get("submitter_id")
            t["biospecimen/samples"].append(scalars(s, case_submitter_id=cid))
            for p in s.get("portions") or []:
                t["biospecimen/portions"].append(scalars(p, case_submitter_id=cid, sample_submitter_id=sid))
                for sl in p.get("slides") or []:
                    t["biospecimen/slides"].append(scalars(sl, case_submitter_id=cid, sample_submitter_id=sid))
                for an in p.get("analytes") or []:
                    t["biospecimen/analytes"].append(scalars(an, case_submitter_id=cid, sample_submitter_id=sid))
                    for al in an.get("aliquots") or []:
                        t["biospecimen/aliquots"].append(scalars(al, case_submitter_id=cid, sample_submitter_id=sid))

    for relpath, rows in t.items():
        _write_tsv(rows, os.path.join(outdir, relpath + ".tsv"))
        print(f"  wrote {relpath}.tsv ({len(rows)} rows)")


# ----------------------------- library API --------------------------------- #
def count_cases(project):
    """Total number of cases (patients) GDC has for a project."""
    filt = json.dumps({"op": "in", "content": {"field": "project.project_id", "value": [project]}})
    params = urllib.parse.urlencode({"filters": filt, "format": "JSON", "size": "0"})
    with urllib.request.urlopen(f"{GDC_CASES}?{params}", timeout=60) as r:
        return json.loads(r.read())["data"]["pagination"]["total"]


def access_breakdown(project):
    """How much of a GDC project is publicly downloadable vs controlled-access.

    Every GDC file is either 'open' (anyone can download) or 'controlled' (needs dbGaP/NIH
    authorization — raw sequencing reads, germline/somatic variants, etc.). This reads only the
    public file *metadata* (available even for controlled files), so it reports what exists behind
    controlled access without needing authorization. Scans all files once (paged) to get exact
    counts AND byte volume, split by access and by data_category.

    Returns:
        dict: {available, project, total_files, open:{files,bytes}, controlled:{files,bytes},
               by_category:[{category, open_files, open_bytes, controlled_files, controlled_bytes}]}.
    """
    from collections import defaultdict
    filt = json.dumps({"op": "in", "content": {"field": "cases.project.project_id", "value": [project]}})
    totals = {"open": [0, 0], "controlled": [0, 0]}  # [files, bytes]
    cats = defaultdict(lambda: {"open": [0, 0], "controlled": [0, 0]})
    offset, page = 0, 10000
    while True:
        params = urllib.parse.urlencode({
            "filters": filt, "fields": "file_size,access,data_category",
            "format": "JSON", "size": str(page), "from": str(offset),
        })
        with urllib.request.urlopen(f"{GDC_FILES}?{params}", timeout=180) as r:
            data = json.loads(r.read())["data"]
        hits = data["hits"]
        for h in hits:
            acc = h.get("access")
            if acc not in totals:
                continue
            size = h.get("file_size") or 0
            cat = h.get("data_category") or "Unknown"
            totals[acc][0] += 1
            totals[acc][1] += size
            cats[cat][acc][0] += 1
            cats[cat][acc][1] += size
        offset += len(hits)
        if not hits or offset >= data["pagination"]["total"]:
            break
    by_category = sorted(
        ({"category": c,
          "open_files": v["open"][0], "open_bytes": v["open"][1],
          "controlled_files": v["controlled"][0], "controlled_bytes": v["controlled"][1]}
         for c, v in cats.items()),
        key=lambda x: -(x["open_files"] + x["controlled_files"]),
    )
    return {
        "available": True,
        "project": project,
        "total_files": totals["open"][0] + totals["controlled"][0],
        "open": {"files": totals["open"][0], "bytes": totals["open"][1]},
        "controlled": {"files": totals["controlled"][0], "bytes": totals["controlled"][1]},
        "by_category": by_category,
    }


def plan(project, dest, limit=6):
    """Select the project's slides and write slides_manifest.tsv to `dest`. No download.

    Args:
        limit: total slides to sample across diagnostic + tissue (split roughly evenly,
            spread by size). 0 or None => ALL slides (a full download).

    Returns:
        (selected, manifest_path). Raises AcquireError if no slides are available.
    """
    diag = query_slides(project, "Diagnostic Slide")
    tiss = query_slides(project, "Tissue Slide")
    if not limit:  # full download — every open tumor slide
        selected = diag + tiss
    else:
        n_diag = (limit + 1) // 2  # odd remainder goes to diagnostic (the pathology-AI ones)
        selected = select_spread(diag, n_diag) + select_spread(tiss, limit - n_diag)
    if not selected:
        raise AcquireError(f"No open-access tumor slides found for {project}.")
    os.makedirs(dest, exist_ok=True)
    manifest = os.path.join(dest, "slides_manifest.tsv")
    write_review_manifest(selected, manifest)
    return selected, manifest


def download(project, dest, selected=None, limit=6, on_progress=None):
    """Download the selected slides + clinical/biospecimen + annotations into `dest`.

    Args:
        selected: pre-selected slides; if None, `plan(project, dest, limit)` chooses them.
        limit: sample size when `selected` is None (0/None = all).
        on_progress: Optional per-file progress callback for the slide phase (see download_selected).
    """
    if selected is None:
        selected, _ = plan(project, dest, limit)
    download_selected(selected, os.path.join(dest, "slides"), on_progress=on_progress)
    fetch_clinical_biospecimen(project, dest)
    fetch_annotations(project, dest)


# ------------------------------- main -------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Catalog-driven GDC acquisition (plan -> review -> download).")
    p.add_argument("--xlsx", default="../../../Data/Data.xlsx")
    p.add_argument("--row", type=int, default=1, help="1-based data row of Dataset Matrix")
    p.add_argument("--project", default=None, help="GDC project id directly (bypass xlsx), e.g. TCGA-STAD")
    p.add_argument("--name", default=None, help="dataset name (used with --project)")
    p.add_argument("--out", default="../../../Data", help="root data folder")
    p.add_argument("--dest", default=None, help="override dataset folder (default: <out>/<project>)")
    p.add_argument("--limit", type=int, default=6, help="total slides to sample (0 = all / full)")
    p.add_argument("--plan", action="store_true", help="select + write manifest only (no download)")
    p.add_argument("--download", action="store_true", help="download the selected files")
    args = p.parse_args()

    if args.project:
        project, name = args.project, (args.name or args.project)
    else:
        row = read_catalog_row(args.xlsx, args.row)
        project, name = resolve_project(row["page"]), row["name"]
    print(f"{name}  ->  GDC project {project}")

    proj_dir = args.dest or os.path.join(args.out, project)
    selected, manifest = plan(project, proj_dir, args.limit)
    print_report(project, selected, manifest)

    if args.download:
        print("Downloading slides + clinical/biospecimen + annotations...")
        download(project, proj_dir, selected)
        print("Done.")


if __name__ == "__main__":
    main()
