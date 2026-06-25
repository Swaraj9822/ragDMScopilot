FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN groupadd -r appuser && useradd -r -m -g appuser appuser

WORKDIR /app

# Install python dependencies
COPY requirements.txt .
# Also copy requirements.lock if it exists (using wildcard to copy it optionally if present in later phases)
COPY requirements.* ./
RUN if [ -f "requirements.lock" ]; then \
      pip install --no-cache-dir -r requirements.lock; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

# Change to non-root user before downloading model so it goes to appuser's home directory
USER appuser

# Pre-download BM25 MS MARCO model to bake it into the image
RUN python -c "from pinecone_text.sparse import BM25Encoder; BM25Encoder.default()"

# Copy application code (with correct ownership)
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser main.py .

# Healthcheck for the API
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Default command (API)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
