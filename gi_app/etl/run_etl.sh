#!/bin/bash
# ETL runner — bootstrap schema, then ingest the datasets on disk under /data.
# Usage (from the etl container):  docker compose run --rm etl bash run_etl.sh

set -e

echo "=========================================="
echo " GI Cancer ETL"
echo "=========================================="

if [ -z "$DATABASE_URL" ]; then
  echo "ERROR: DATABASE_URL is not set (check .env / docker-compose)."
  exit 1
fi

# Wait for Postgres.
echo "Waiting for database..."
MAX_RETRIES=30
RETRY_COUNT=0
until python -c "from Database.database import engine; from sqlalchemy import text; engine.connect().execute(text('SELECT 1'))" 2>/dev/null; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  if [ $RETRY_COUNT -ge $MAX_RETRIES ]; then
    echo "ERROR: database not reachable after $MAX_RETRIES attempts."
    exit 1
  fi
  echo "  not ready (attempt $RETRY_COUNT/$MAX_RETRIES), retrying in 2s..."
  sleep 2
done
echo "Database is ready."

echo ""; echo "== Step 1: init schema (tables + views) =="
python /shared/Database/init_db.py

echo ""; echo "== Step 2: ingest TCGA-COAD clinical + biospecimen + annotations =="
python ingest/tcga_ingest.py

echo ""; echo "== Step 3: upload slides to object storage + register data_assets =="
python ingest/slide_ingest.py

echo ""; echo "== Step 4: upload pathology-report PDFs + register data_assets =="
# No-ops with a warning when the reports were never fetched; run
# `connectors/gdc_reports.py --project TCGA-COAD --download` to populate them.
python ingest/report_ingest.py

echo ""
echo "=========================================="
echo " ETL completed successfully"
echo "=========================================="
