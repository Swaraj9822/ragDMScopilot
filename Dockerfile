# Backend image — shared by the API and the ingestion worker.
# The two services run the same code with different commands (see compose).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# curl is used by the container healthcheck; build-essential covers the rare
# wheel that needs to compile from source. Removed from the layer afterwards.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && apt-get purge -y build-essential && apt-get autoremove -y

COPY src ./src
COPY config ./config
COPY main.py ./

EXPOSE 8000

# Default command runs the HTTP API. The worker service overrides this in
# docker-compose with: python -m rag_system.worker
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
