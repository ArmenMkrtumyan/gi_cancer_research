# GI Cancer Platform — Data Layer (`gi_app/`)

A walkthrough of what this directory is, how every file fits together, how to run it,
and how to inspect the data. This is Phase 1: the **data layer**. The web tool (FastAPI
API + React frontend) comes next and is only scaffolded here.

> Source of truth for the database design is [`../docs/schema.dbml`](../docs/schema.dbml)
> (+ `../docs/schema_logs.md` and `../docs/ARCHITECTURE.md`). This README explains the
> *implementation* of that design.

## Contents
1. [The big picture](#1-the-big-picture)
2. [File structure](#2-file-structure)
3. [Configuration files](#3-configuration-files)
4. [The shared DB layer](#4-the-shared-db-layer)
5. [The ETL pipeline](#5-the-etl-pipeline)
6. [The other services (scaffolds)](#6-the-other-services-scaffolds)
7. [Running it: `docker compose` up / down](#7-running-it-docker-compose-up--down)
8. [Inspecting the data in pgAdmin](#8-inspecting-the-data-in-pgadmin)

---

## 1. The big picture

This is a small **data platform** running as a set of cooperating Docker containers.

```
        Data/TCGA_COAD/*.tsv + *.svs   (raw downloaded files)
                     │
                     ▼
   ┌──────────────  etl container  ──────────────┐
   │  reads TSVs → normalizes → writes rows        │
   │  uploads .svs files → object storage          │
   └───────────┬───────────────────────┬───────────┘
               ▼                        ▼
        Postgres (db)              MinIO (S3-compatible)
        structured data            big binary files (.svs)
               │
        pgAdmin (browser UI to inspect the DB)
        notebook (Jupyter, for analysis)
```

The **relational data** (patients, diagnoses, samples, slide metadata) lives in
**Postgres**. The **large slide images** (`.svs`, gigabytes) never enter the database —
they live in **MinIO** (object storage), and Postgres holds only a *pointer* row
(`data_assets`) with the `s3://…` address. This is what lets the system scale to
terabytes later without bloating the database, and it swaps to real AWS S3 with only an
endpoint change.

Everything is **Dockerized** so the whole stack starts with one command and behaves the
same on a laptop as it will on AWS.

---

## 2. File structure

```
gi_app/
├── docker-compose.yaml        # defines all the services (containers)
├── .env.example               # template for secrets/config; copy to .env
│
├── shared/                    # code shared by every Python service (mounted at /shared)
│   ├── requirements-base.txt  # Python deps common to all services
│   ├── storage.py             # MinIO/S3 helper
│   ├── logging_setup.py       # colored stdlib logging config + get_splitter banner
│   └── Database/
│       ├── __init__.py        # marks this a Python package
│       ├── database.py        # DB connection (engine, session, get_db)
│       ├── models.py          # the 15 tables as Python classes
│       ├── schema.py          # Pydantic shapes (for the API later)
│       ├── init_db.py         # creates tables + the 2 views
│       └── utils.py           # DB helpers (get_or_create_dataset)
│
├── etl/                       # the data-loading pipeline
│   ├── Dockerfile             # how to build the etl container image
│   ├── requirements.txt       # etl-only Python deps
│   ├── run_etl.sh             # orchestrates the whole load
│   ├── etl_utils.py           # shared parsing/normalization helpers
│   ├── ingest/                # the DB loaders (one per source, grows over time)
│   │   ├── tcga_ingest.py     # loads TCGA-COAD TSVs → Postgres
│   │   └── slide_ingest.py    # uploads .svs → MinIO, registers data_assets
│   └── connectors/            # pre-existing acquisition tools (gdc_*.py)
│
├── notebook/                  # Jupyter service (Dockerfile, requirements, starter.ipynb)
├── api/                       # FastAPI backend — scaffold for the web-tool phase
└── app/                       # React frontend — placeholder for next phase
```

---

## 3. Configuration files

### `docker-compose.yaml`
The **orchestra conductor** — declares every container, how to build it, what ports it
exposes, and how they depend on each other.

- **YAML anchors (top of file).** `x-database-env` and `x-db-dependency` are reusable
  snippets so the same env vars and dependencies aren't repeated per service.
- **`services:`**:
  - **`db`**
  - **`pgadmin`**
  - **`minio`** — S3-compatible object store.
  - **`minio-init`** — a *one-shot* `mc` container: waits for MinIO, creates the
    `gi-cancer` bucket + `bronze/silver/gold` prefixes, then `exit 0` (so it correctly
    shows as **Exited (0)** — it runs once and stops).
  - **`etl`**
  - **`notebook`**
  - **`api` / `app`** — start with the rest of the stack and power the local dashboard.
- **`volumes:`**
  - `postgres_data`
  - `pgadmin_data`
  - `minio_data`

### `.env`
Configuration and secrets

---

## 4. The shared DB layer

Lives **once** in `shared/` and is mounted into every Python container — one source of
truth for the models, no per-service duplication.

### `shared/Database/database.py`
**How the code talks to Postgres.** It reads where the database is (`DATABASE_URL`) and
opens the connection. It gives the rest of the code three things:
- `engine` — the live connection to Postgres.
- `SessionLocal` — whenever we need to read or write.
- `Base` — the base class every table model inherits from.

### `shared/Database/models.py`
The **15 tables as Python classes** — a direct translation of `schema.dbml`.

| Table | What it holds |
|---|---|
| `Dataset` | A source dataset in the catalog (e.g. TCGA-COAD or a GEO series). |
| `IngestionRun` | Audit log: one row per ETL run for a dataset. |
| `Case` | A patient. The centre of the schema — most tables link back here. |
| `Diagnosis` | A cancer diagnosis for a case; holds staging/TNM. |
| `Treatment` | A treatment given for a diagnosis (chemo, radiation, surgery, ...). |
| `PathologyDetail` | Pathologist's specimen findings for a diagnosis (node counts, invasion markers). |
| `FollowUp` | A follow-up timepoint for a case; feeds survival. |
| `MolecularTest` | A clinical molecular/biomarker test result (MMR, MSI, KRAS, CEA). |
| `Sample` | A physical specimen taken from a patient. |
| `Portion` | A piece cut from a sample (part of the molecular-prep tree). |
| `Analyte` | Extracted DNA/RNA from a portion, with quality metrics. |
| `Aliquot` | A measured sub-portion of an analyte prepared for an assay. |
| `Slide` | Pathologist metadata for a slide (tumor %, section); the .svs image is a `DataAsset`. |
| `Annotation` | A GDC QC/administrative flag, resolved to a case. |
| `DataAsset` | Pointer to a file in object storage (e.g. a .svs slide in MinIO/S3). |

### `shared/Database/init_db.py`
**Sets up the database.** Run once at the start; safe to run again (it skips anything that
already exists, so it won't wipe existing data). It does two things:

1. **Creates the tables** — turns the 15 classes in `models.py` into real, empty tables in
   Postgres.
2. **Creates two "views"** — a view is a saved query that acts like a read-only table but
   recalculates every time it's read, so it's always up to date:
   - `dataset_stats` — how many patients and files each dataset has.
   - `case_survival` — for each patient, how long they survived and whether they died (uses
     only their main GI diagnosis, so unrelated cancers don't skew the numbers).

### `shared/Database/schema.py`
**Pydantic** models describing the JSON shapes the API will use. Minimal for now
(`Dataset`, `DataAsset`); `from_attributes = True` lets Pydantic read SQLAlchemy objects
directly. Filled out in the web-tool phase.

### `shared/Database/utils.py`
Database helper functions.
Holds `get_or_create_dataset` — the small helper each loader calls to register its own
dataset in the `datasets` table (idempotent, matched by name).

### `shared/storage.py`
**Handles the big files** (the `.svs` slide images) — uploading and reading them from
object storage. Today it talks to the local MinIO; on AWS it talks to S3. The rest of the
code just calls its helpers and doesn't care which:
- `put_file` — upload a file.
- `exists` — check whether a file is there.
- `build_uri` — build the file's address (`s3://gi-cancer/…`).
- `url_for` — make a temporary download link.

---

## 5. The ETL pipeline

### `etl/Dockerfile`

### `etl/requirements.txt`

### `etl/run_etl.sh`
**The one command to run to load the data.**:

1. `init_db.py` — set up the tables and views.
2. `tcga_ingest.py` — load the TCGA-COAD clinical + biospecimen data into Postgres.
3. `slide_ingest.py` — upload the `.svs` slide images to MinIO and register them.

### `etl/etl_utils.py`

### `etl/ingest/tcga_ingest.py`
The **main loader** — reads the TCGA-COAD TSVs and writes them into the clinical,
biospecimen, and curation tables. It wipes and reloads each run (so re-running is safe)
and applies the schema's cleaning rules along the way.

### `etl/ingest/slide_ingest.py`
Uploads the `.svs` slide images to object storage and records each one as a `DataAsset`
row pointing at it. Also safe to re-run.

### `etl/connectors/`
Pre-existing **acquisition** tools (`gdc_acquire.py`, etc.) — they *download* raw data;
the `*_ingest.py` files *load* it. Separate concerns.

---

## 6. The other services (scaffolds)

- **`notebook/`** — Jupyter Dockerfile + requirements (jupyterlab, matplotlib, duckdb,
  pyarrow) + `starter.ipynb` that connects to the DB and shows `dataset_stats`. The
  analysis playground.
- **`api/main.py`** — FastAPI backend for the first ingested-data dashboard. It exposes
  `/health`, `/datasets`, `/stats/overview`,
  `/stats/dataset-stats`, dataset summary/survival/missingness/linkage endpoints,
  asset endpoints, signed download URLs, and ingestion runs.
- **`app/`** — static nginx-served dashboard for already-ingested data. It shows the
  `dataset_stats` and `case_survival` views, metadata completeness, linkage checks,
  downloaded WSI assets, and ETL runs.

---

## 7. Running it: `docker compose` up / down

All commands run from inside `gi_app/`.

```bash
docker compose up -d                          # start the stack
docker compose run --rm etl bash run_etl.sh   # load the data
docker compose ps -a                          # check status
docker compose down                           # stop (data is kept)
```

- Add `--build` after changing a Dockerfile or requirements file (not needed for `.py` edits).
- `docker compose down -v` also deletes the data volumes — a completely fresh start.

### Docker Desktop credential-helper note

If Docker fails while pulling/building with:

```text
exec: "docker-credential-desktop": executable file not found in $PATH
```

Docker Desktop's credential helper exists, but your shell cannot find it. For the current
terminal session, run:

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
```

Then rerun `docker compose up -d --build`. Add the same export to `~/.zshrc` if you want
it to persist for future terminals.

---

## 8. Inspecting the data in pgAdmin

**Log in:** open **http://localhost:5050** and sign in with `PGADMIN_EMAIL` / `PGADMIN_PASSWORD` from `.env`.

**Register the database (first time only):** right-click **Servers → Register → Server…**

*General* tab:
- **Name** — any label, e.g. `gi_cancer`. This is just the name in the sidebar (the
  "Name cannot be empty" error is only asking for this).

*Connection* tab — values come from `.env`:

| Field | Value |
|---|---|
| Host name/address | `db` — the container name, **not** `localhost` (pgAdmin runs inside Docker, so `localhost` would point at pgAdmin itself) |
| Port | `5432` |
| Maintenance database | `DB_NAME` (default `gi_cancer_db`) |
| Username | `DB_USER` |
| Password | `DB_PASSWORD` |

Leave the other fields at their defaults and click **Save**.

**Browse / query:** in the left **Object Explorer** tree, expand **gi_cancer → Databases →
gi_cancer_db → Schemas → public → Tables** to see the 15 tables. To run SQL, click
**`gi_cancer_db`** to select it, then right-click it → **Query Tool**:

```sql
-- Catalog & provenance
SELECT * FROM datasets;
SELECT * FROM ingestion_runs;

-- Clinical (per patient)
SELECT * FROM cases;
SELECT * FROM diagnoses;
SELECT * FROM treatments;
SELECT * FROM pathology_details;
SELECT * FROM follow_ups;
SELECT * FROM molecular_tests;

-- Biospecimen (per sample)
SELECT * FROM samples;
SELECT * FROM portions;
SELECT * FROM analytes;
SELECT * FROM aliquots;
SELECT * FROM slides;

-- Curation
SELECT * FROM annotations;

-- File pointers (object storage)
SELECT * FROM data_assets;

-- Derived views
SELECT * FROM dataset_stats;
SELECT * FROM case_survival;
```

### Browsing the files in MinIO

Open **http://localhost:9001** and log in with `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`
from `.env`. Go to **Object Browser → `gi-cancer` → `bronze/TCGA-COAD/slides/`** to see the
uploaded `.svs` slide images.

Jupyter (notebook service): http://localhost:8888.

### Opening the web dashboard

Start the stack, then open:

- API docs: http://localhost:8008/docs
- Dashboard: http://localhost:5173

The dashboard asks the API for signed MinIO links when you click a WSI asset. Locally,
the API rewrites those signed links to `S3_PUBLIC_ENDPOINT` (default
`http://localhost:9000`) so they work from your browser.
