#!/usr/bin/env python3
"""
GDC pathology-report fetcher — download the open-access pathologist's report
PDFs for a TCGA project, md5-verified. Stdlib-only (no pip deps).

Why this is its own tool:
  The biospecimen `samples.tsv` gives a `pathology_report_uuid` per sample, but
  that UUID is NOT a downloadable file id — it is an internal tag embedded in the
  report's file name (e.g. TCGA-AA-3562.98738F18-...-.PDF). The actual PDF is a
  separate GDC file with `data_type = "Pathology Report"` and its own `file_id`.
  So we query the /files API by (project, data_type=Pathology Report, open) to get
  the real file ids, then stream them from /data. Two-phase like gdc_acquire:
    --plan (writes reports_manifest.tsv for review)  ->  --download (fetch + verify)

Examples:
    # See what report PDFs exist for TCGA-COAD (writes a manifest, no download):
    python gdc_reports.py --project TCGA-COAD

    # Download them all into Data/TCGA-COAD/pathology_reports/, md5-verified:
    python gdc_reports.py --project TCGA-COAD --download

    # Only the cases that appear in an existing samples.tsv (e.g. our sample cohort):
    python gdc_reports.py --project TCGA-COAD --download \
        --only-cases Data/TCGA_COAD/biospecimen/samples.tsv
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import urllib.request

GDC_FILES_ENDPOINT = "https://api.gdc.cancer.gov/files"
GDC_DATA_ENDPOINT = "https://api.gdc.cancer.gov/data"


def _in(field, values):
    return {"op": "in", "content": {"field": field, "value": list(values)}}


def query_reports(project, limit):
    """Return the open-access Pathology Report file hits for a project."""
    payload = {
        "filters": {"op": "and", "content": [
            _in("cases.project.project_id", [project]),
            _in("data_type", ["Pathology Report"]),
            _in("access", ["open"]),
        ]},
        "fields": ",".join([
            "file_id", "file_name", "md5sum", "file_size", "access",
            "cases.submitter_id",
        ]),
        "format": "JSON",
        "size": str(limit),
        "sort": "file_size:asc",
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        GDC_FILES_ENDPOINT, data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    return data["data"]["hits"]


def report_uuid_from_name(file_name):
    """The internal report uuid GDC embeds in the PDF name: <barcode>.<UUID>.PDF"""
    parts = file_name.rsplit(".", 1)[0].split(".")
    return parts[-1] if len(parts) >= 2 else ""


def rows_from_hits(hits):
    """Flatten hits into manifest rows (one per report file)."""
    rows = []
    for h in hits:
        case = (h.get("cases") or [{}])[0].get("submitter_id", "")
        rows.append({
            "case": case,
            "pathology_report_uuid": report_uuid_from_name(h["file_name"]),
            "file_name": h["file_name"],
            "file_id": h["file_id"],
            "md5": h.get("md5sum", ""),
            "size_bytes": str(h.get("file_size", "")),
        })
    rows.sort(key=lambda r: r["case"])
    return rows


def load_case_filter(samples_tsv):
    """Distinct case barcodes present in a samples.tsv (to restrict the pull)."""
    with open(samples_tsv, newline="") as f:
        r = csv.DictReader(f, delimiter="\t")
        return {row["case_submitter_id"] for row in r if row.get("case_submitter_id")}


def write_manifest(rows, path):
    cols = ["case", "pathology_report_uuid", "file_name", "file_id", "md5", "size_bytes"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
        w.writeheader()
        w.writerows(rows)


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(rows, outdir):
    os.makedirs(outdir, exist_ok=True)
    ok = 0
    for i, r in enumerate(rows, 1):
        dest = os.path.join(outdir, r["file_name"])
        size_kb = int(r["size_bytes"] or 0) / 1e3
        print(f"  [{i}/{len(rows)}] {r['file_name']} ({size_kb:.0f} KB) ...", flush=True)
        url = f"{GDC_DATA_ENDPOINT}/{r['file_id']}"
        with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                out.write(chunk)
        if r["md5"] and _md5(dest) != r["md5"]:
            print(f"      !! md5 MISMATCH — deleting {r['file_name']}", file=sys.stderr)
            os.remove(dest)
            continue
        ok += 1
    print(f"Downloaded + md5-verified {ok}/{len(rows)} report(s) -> {outdir}")


def main():
    p = argparse.ArgumentParser(description="Fetch open-access GDC pathology-report PDFs (manifest + optional download).")
    p.add_argument("--project", default="TCGA-COAD", help="GDC project_id, e.g. TCGA-COAD")
    p.add_argument("--limit", type=int, default=2000, help="max report files to consider")
    p.add_argument("--only-cases", default=None, metavar="samples.tsv",
                   help="restrict to case barcodes present in this samples.tsv")
    p.add_argument("--manifest", default=None, help="manifest path (default: <out>/reports_manifest.tsv)")
    p.add_argument("--download", action="store_true", help="also download the PDFs")
    p.add_argument("--out", default=None, help="output dir (default: Data/<project>/pathology_reports)")
    args = p.parse_args()

    print(f"Querying GDC pathology reports: project={args.project} (open access)")
    hits = query_reports(args.project, args.limit)
    rows = rows_from_hits(hits)
    if not rows:
        print("No open pathology-report files matched.", file=sys.stderr)
        sys.exit(1)

    if args.only_cases:
        keep = load_case_filter(args.only_cases)
        before = len(rows)
        rows = [r for r in rows if r["case"] in keep]
        print(f"Restricted to {len(keep)} case(s) from {args.only_cases}: {before} -> {len(rows)} report(s)")

    total_mb = sum(int(r["size_bytes"] or 0) for r in rows) / 1e6
    print(f"Matched {len(rows)} report(s), total {total_mb:.1f} MB. Sample:")
    for r in rows[:5]:
        print(f"  - {r['case']}  {r['file_name']}  ({int(r['size_bytes'] or 0)/1e3:.0f} KB)")

    outdir = args.out or os.path.join("Data", args.project.replace("-", "_"), "pathology_reports")
    os.makedirs(outdir, exist_ok=True)
    manifest = args.manifest or os.path.join(outdir, "reports_manifest.tsv")
    write_manifest(rows, manifest)
    print(f"Wrote manifest -> {manifest}")

    if args.download:
        download(rows, outdir)
    else:
        print("Review the manifest, then re-run with --download to fetch the PDFs.")


if __name__ == "__main__":
    main()
