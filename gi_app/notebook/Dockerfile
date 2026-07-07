FROM python:3.12-slim-bookworm

# All deps (jupyterlab, matplotlib, pyarrow, duckdb, psycopg2-binary, ...) ship
# prebuilt wheels; no compiler/apt packages required.
WORKDIR /notebook

COPY shared/requirements-base.txt /tmp/requirements-base.txt
RUN pip3 install --upgrade pip && pip3 install -r /tmp/requirements-base.txt

COPY notebook/requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

ENV PYTHONPATH=/shared:/etl

EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", \
     "--allow-root", "--ServerApp.token=", "--ServerApp.password="]
