FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# XGBoost (pricing.py) needs the OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY migrations migrations
COPY ingest ingest
# Editable install keeps alembic.ini and migrations/ next to the package.
RUN pip install -e .

# Runs as the host user (set by docker-compose) so files in ./data stay owned by you.
USER 10001
CMD ["python", "-m", "ingest.worker"]
