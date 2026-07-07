FROM python:3.12-slim-bookworm

# No apt build tools needed: psycopg2-binary, pandas, numpy, pyarrow, duckdb, boto3,
# openpyxl all ship prebuilt wheels for linux/arm64.
WORKDIR /etl

# Shared base requirements first (cached across services), then ETL-specific.
COPY shared/requirements-base.txt /tmp/requirements-base.txt
RUN pip3 install --upgrade pip && pip3 install -r /tmp/requirements-base.txt

COPY etl/requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY etl/ .
RUN chmod +x run_etl.sh

# Models/storage live in /shared (mounted); make them importable.
ENV PYTHONPATH=/shared:/etl

# ETL runs on demand: docker compose run --rm etl bash run_etl.sh
CMD ["tail", "-f", "/dev/null"]
