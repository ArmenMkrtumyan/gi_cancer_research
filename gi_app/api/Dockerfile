FROM python:3.12-slim-bookworm

# psycopg2-binary + fastapi/uvicorn/pydantic/boto3 all ship prebuilt wheels.
WORKDIR /api

COPY shared/requirements-base.txt /tmp/requirements-base.txt
RUN pip3 install --upgrade pip && pip3 install -r /tmp/requirements-base.txt

COPY api/requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY api/ .

ENV PYTHONPATH=/shared:/etl

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
