# --- Builder stage ---
FROM python:3.14.8-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88 AS builder

WORKDIR /build

COPY containers/dispatch/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src/ ./src/
# README.md is pyproject's `readme`; without it setuptools warns and builds
# metadata with an empty long description.
COPY pyproject.toml README.md ./
# [observability] so init_tracing() in the lifespan can export to Cloud Trace
# instead of always taking its ImportError branch.
RUN pip install --no-cache-dir --prefix=/install -e ".[observability]"

# --- Runtime stage ---
FROM python:3.14.8-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88

# bash runs entrypoint.sh. Dispatch only normalizes and publishes: repository
# indexing (which needs git) runs in Mastermind's image.
RUN apt-get update && apt-get install -y --no-install-recommends bash && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

RUN groupadd --gid 1001 henchmen && useradd --uid 1001 --gid henchmen --shell /bin/bash --create-home henchmen

WORKDIR /app

COPY src/ ./src/
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --no-deps -e .

COPY containers/dispatch/entrypoint.sh .
RUN chmod +x entrypoint.sh && chown -R henchmen:henchmen /app

USER henchmen

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status == 200 else 1)"]

CMD ["./entrypoint.sh"]
