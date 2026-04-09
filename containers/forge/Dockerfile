# --- Builder stage ---
# TODO: pin to actual digest before first release
FROM python:3.12.8-slim-bookworm@sha256:9c18f9a24c7e0a3d03fd0c7e8f3a6c2a7c0d7e2f5b3b1a0b1b1a0b1b1a0b1b1a AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY containers/forge/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src/ ./src/
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install -e .

# --- Runtime stage ---
# TODO: pin to actual digest before first release
FROM python:3.12.8-slim-bookworm@sha256:9c18f9a24c7e0a3d03fd0c7e8f3a6c2a7c0d7e2f5b3b1a0b1b1a0b1b1a0b1b1a

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

RUN groupadd --gid 1001 henchmen && useradd --uid 1001 --gid henchmen --shell /bin/bash --create-home henchmen

WORKDIR /app

COPY src/ ./src/
COPY pyproject.toml .
RUN pip install --no-cache-dir --no-deps -e .

RUN chown -R henchmen:henchmen /app

USER henchmen

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status == 200 else 1)"]

CMD ["uvicorn", "henchmen.forge.server:app", "--host", "0.0.0.0", "--port", "8080"]
