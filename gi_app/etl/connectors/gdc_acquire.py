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

import openpyxl

GDC_FILES = "https://api.gdc.cancer.gov/files"
GDC_DATA = "https://api.gdc.cancer.gov/data"
GDC_ANN = "https://api.gdc.cancer.gov/annotations"
SHEET = "Dataset Matrix"


# ----------------------------- Data.xlsx ----------------------------------- #
def read_catalog_row(xlsx_path, row_1based):
    """Return {name, page, source} for the Nth data row of the Dataset Matrix,
    skipping blank separator rows."""
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
        sys.exit(f"Row is not a GDC/TCGA dataset (page={page_url!r}); "
                 "GEO connector handles those.")
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
    for h in hits:
        h["_ann"] = len(h.get("annotations") or [])
        h["_size"] = h.get("file_size", 0)
        h["_barcode"] = h["file_name"].split(".")[0]
        h["_case"] = (h.get("cases") or [{}])[0].get("submitter_id", "?")
    return hits


def select_triple(hits, noann_bucket="middle"):
    """Pick 3 slides: 2 with annotation + 1 without, spread by size.
    noann_bucket controls which size slot the non-annotated file fills."""
    withann = sorted([h for h in hits if h["_ann"]], key=lambda h: h["_size"])
    without = sorted([h for h in hits if not h["_ann"]], key=lambda h: h["_size"])
    if len(withann) < 2 or len(without) < 1:
        sys.exit(f"Not enough slides to satisfy 2+1 (with={len(withann)}, without={len(without)})")
    median = lambda xs: xs[len(xs) // 2]
    if noann_bucket == "middle":      # annotated = small + large, non-ann = middle
        picks = [withann[0], median(without), withann[-1]]
    elif noann_bucket == "largest":   # annotated = small + middle, non-ann = largest
        picks = [withann[0], median(withann), without[-1]]
    elif noann_bucket == "smallest":  # non-ann = smallest, annotated = middle + large
        picks = [without[0], median(withann), withann[-1]]
    else:
        sys.exit(f"bad noann_bucket {noann_bucket!r}")
    for slot, h in zip(["smallest", "middle", "largest"], picks):
        h["_slot"] = slot
    return picks


# ----------------------------- outputs ------------------------------------- #
def write_manifest(selected, path):
    """gdc-client compatible manifest (minimal)."""
    lines = ["id\tfilename\tmd5\tsize\tstate"]
    for h in selected:
        lines.append(f"{h['file_id']}\t{h['file_name']}\t{h.get('md5sum','')}\t{h['_size']}\treleased")
    open(path, "w").write("\n".join(lines) + "\n")


def write_review_manifest(groups, path):
    """Human-readable slide manifest (TSV). Annotation data is intentionally NOT
    here — it lives in annotations.tsv (its own table), joined by `case`."""
    cols = ["slide_type", "size_mb", "case", "barcode",
            "file_name", "file_id", "md5"]
    lines = ["\t".join(cols)]
    for strategy, picks in groups.items():
        stype = "diagnostic" if "Diagnostic" in strategy else "tissue"
        for h in picks:
            lines.append("\t".join([
                stype, f"{h['_size']/1e6:.1f}",
                h["_case"], h["_barcode"], h["file_name"], h["file_id"],
                h.get("md5sum", ""),
            ]))
    open(path, "w").write("\n".join(lines) + "\n")


def print_report(project, groups, review_path):
    print(f"\n{'='*90}\n  SELECTION PLAN — {project}\n{'='*90}")
    for strategy, picks in groups.items():
        print(f"\n  {strategy}:")
        print(f"    {'slot':9} {'ann*':5} {'size':>9}  {'case':14} file")
        for h in picks:
            print(f"    {h['_slot']:9} {('yes' if h['_ann'] else 'no'):5} "
                  f"{h['_size']/1e6:7.1f}MB  {h['_case']:14} {h['file_name'][:44]}")
    total = sum(h["_size"] for picks in groups.values() for h in picks)
    n = sum(len(p) for p in groups.values())
    print(f"\n  * 'ann' = selection used GDC annotation presence (2 with + 1 without per type).")
    print(f"    The annotation DATA lives in annotations.tsv (its own table), joined by `case`.")
    print(f"  + normalized clinical/ + biospecimen/ + annotations.tsv (fetched on download)")
    print(f"  slide total: {total/1e9:.2f} GB across {n} files")
    print(f"  manifest:  {review_path}  (slide files only — no annotation columns)")
    print(f"\n  Review the files above. To download, re-run with --download\n")


# ----------------------------- download ------------------------------------ #
def download_selected(selected, outdir):
    import hashlib
    os.makedirs(outdir, exist_ok=True)
    for i, h in enumerate(selected, 1):
        dest = os.path.join(outdir, h["file_name"])
        if os.path.exists(dest) and os.path.getsize(dest) == h["_size"]:
            print(f"  [{i}/{len(selected)}] exists, skipping {h['file_name']}")
            continue
        print(f"  [{i}/{len(selected)}] {h['file_name']} ({h['_size']/1e6:.0f} MB)...", flush=True)
        m = hashlib.md5()
        with urllib.request.urlopen(f"{GDC_DATA}/{h['file_id']}", timeout=300) as r, open(dest, "wb") as o:
            while True:
                c = r.read(1 << 20)
                if not c:
                    break
                o.write(c); m.update(c)
        if h.get("md5sum") and m.hexdigest() != h["md5sum"]:
            print(f"      !! md5 MISMATCH for {h['file_name']}", file=sys.stderr)


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


# ------------------------------- main -------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Catalog-driven GDC acquisition (plan -> review -> download).")
    p.add_argument("--xlsx", default="../../../Data/Data.xlsx")
    p.add_argument("--row", type=int, default=1, help="1-based data row of Dataset Matrix")
    p.add_argument("--out", default="../../../Data", help="root data folder")
    p.add_argument("--dest", default=None, help="override dataset folder (default: <out>/<project>)")
    p.add_argument("--noann-bucket", choices=["smallest", "middle", "largest"], default="middle",
                   help="which size slot the non-annotated slide fills")
    p.add_argument("--plan", action="store_true", help="select + write manifest only (no download)")
    p.add_argument("--download", action="store_true", help="download the selected files")
    args = p.parse_args()

    row = read_catalog_row(args.xlsx, args.row)
    project = resolve_project(row["page"])
    print(f"Row {args.row}: {row['name']}  ->  GDC project {project}")

    groups = {}
    for strategy in ("Diagnostic Slide", "Tissue Slide"):
        groups[strategy] = select_triple(query_slides(project, strategy), args.noann_bucket)

    selected = [h for picks in groups.values() for h in picks]
    proj_dir = args.dest or os.path.join(args.out, project)
    os.makedirs(proj_dir, exist_ok=True)
    review_manifest = os.path.join(proj_dir, "slides_manifest.tsv")
    write_review_manifest(groups, review_manifest)
    print_report(project, groups, review_manifest)

    if args.download:
        print("Downloading slides...")
        download_selected(selected, os.path.join(proj_dir, "slides"))
        print("Fetching clinical + biospecimen TSVs...")
        fetch_clinical_biospecimen(project, proj_dir)
        print("Fetching annotations...")
        fetch_annotations(project, proj_dir)
        print("Done.")


if __name__ == "__main__":
    main()
