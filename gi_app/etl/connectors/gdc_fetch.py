#!/usr/bin/env python3
"""
GDC fetcher — query the GDC REST API for a filtered set of files, write a
gdc-client manifest, and (optionally) download them. Stdlib-only (no pip deps).

This exists because the GDC Data Portal UI cannot filter the Repository by
project via "Add a Custom Filter" (project is a *case* property, not a *file*
property). The REST API has no such limitation, so we filter there instead.

Examples:
    # Build a manifest for 8 open-access diagnostic slides of TCGA-COAD:
    python gdc_fetch.py --project TCGA-COAD --slide-type diagnostic --limit 8

    # Same, but also download the .svs files into the landing folder:
    python gdc_fetch.py --project TCGA-COAD --slide-type diagnostic --limit 8 \
        --download --out ../landing/TCGA-COAD/slides

    # Tumor, primary only:
    python gdc_fetch.py --project TCGA-COAD --slide-type diagnostic --limit 8 \
        --tissue-type tumor --tumor-descriptor primary
"""

import argparse
import json
import os
import sys
import urllib.request

GDC_FILES_ENDPOINT = "https://api.gdc.cancer.gov/files"
GDC_DATA_ENDPOINT = "https://api.gdc.cancer.gov/data"

SLIDE_STRATEGY = {
    "diagnostic": "Diagnostic Slide",  # FFPE H&E — the pathology-AI standard
    "tissue": "Tissue Slide",          # frozen/OCT — noisier
}


def _eq(field, value):
    return {"op": "in", "content": {"field": field, "value": [value]}}


def build_filters(args):
    """Compose the GDC filter JSON from CLI args."""
    content = [
        _eq("cases.project.project_id", args.project),
        _eq("data_type", "Slide Image"),
        _eq("experimental_strategy", SLIDE_STRATEGY[args.slide_type]),
        _eq("access", "open"),
    ]
    if args.tissue_type:
        content.append(_eq("cases.samples.tissue_type", args.tissue_type))
    if args.tumor_descriptor:
        content.append(_eq("cases.samples.tumor_descriptor", args.tumor_descriptor))
    return {"op": "and", "content": content}


def query_files(args):
    """POST the filter to the GDC /files endpoint and return the file hits."""
    payload = {
        "filters": build_filters(args),
        # entity_submitter_id gives us the TCGA barcode for slide->case linkage
        "fields": ",".join([
            "file_id", "file_name", "md5sum", "file_size", "state",
            "cases.submitter_id",
            "cases.samples.portions.analytes.aliquots.submitter_id",
        ]),
        "format": "JSON",
        "size": str(args.limit),
        "sort": "file_size:asc",  # smallest first — gentler on a laptop
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        GDC_FILES_ENDPOINT, data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["data"]["hits"]


def write_manifest(hits, path):
    """Write a gdc-client-compatible manifest (TSV)."""
    lines = ["id\tfilename\tmd5\tsize\tstate"]
    for h in hits:
        lines.append("\t".join([
            h["file_id"], h["file_name"], h.get("md5sum", ""),
            str(h.get("file_size", "")), h.get("state", ""),
        ]))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def download(hits, outdir):
    """Stream each file from the GDC data endpoint into outdir."""
    os.makedirs(outdir, exist_ok=True)
    for i, h in enumerate(hits, 1):
        dest = os.path.join(outdir, h["file_name"])
        url = f"{GDC_DATA_ENDPOINT}/{h['file_id']}"
        size_mb = h.get("file_size", 0) / 1e6
        print(f"  [{i}/{len(hits)}] {h['file_name']} ({size_mb:.1f} MB) ...", flush=True)
        with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)  # 1 MB
                if not chunk:
                    break
                out.write(chunk)
    print(f"Downloaded {len(hits)} file(s) -> {outdir}")


def main():
    p = argparse.ArgumentParser(description="Fetch filtered GDC files (manifest + optional download).")
    p.add_argument("--project", default="TCGA-COAD", help="GDC project_id, e.g. TCGA-COAD")
    p.add_argument("--slide-type", choices=SLIDE_STRATEGY, default="diagnostic")
    p.add_argument("--limit", type=int, default=8, help="max files to fetch")
    p.add_argument("--tissue-type", choices=["tumor", "normal"], default=None)
    p.add_argument("--tumor-descriptor", default=None, help="e.g. primary, metastatic")
    p.add_argument("--manifest", default=None, help="manifest output path (default: ./gdc_manifest_<project>.txt)")
    p.add_argument("--download", action="store_true", help="also download the files")
    p.add_argument("--out", default=None, help="download dir (default: landing/<project>/slides)")
    args = p.parse_args()

    print(f"Querying GDC: project={args.project} strategy={SLIDE_STRATEGY[args.slide_type]} limit={args.limit}")
    hits = query_files(args)
    if not hits:
        print("No files matched. Check the project name / filters.", file=sys.stderr)
        sys.exit(1)

    total_gb = sum(h.get("file_size", 0) for h in hits) / 1e9
    print(f"Matched {len(hits)} file(s), total {total_gb:.2f} GB:")
    for h in hits:
        barcode = h.get("cases", [{}])[0].get("submitter_id", "?")
        print(f"  - {h['file_name']}  [{barcode}]  {h.get('file_size', 0)/1e6:.1f} MB")

    manifest = args.manifest or f"gdc_manifest_{args.project}.txt"
    write_manifest(hits, manifest)
    print(f"Wrote manifest -> {manifest}")
    print(f"  (download later with: gdc-client download -m {manifest} -d <dir>)")

    if args.download:
        outdir = args.out or os.path.join("landing", args.project, "slides")
        download(hits, outdir)


if __name__ == "__main__":
    main()
