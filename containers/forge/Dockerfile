# --- Builder stage ---
FROM python:3.14.7-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY containers/forge/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src/ ./src/
# README.md is pyproject's `readme`; without it setuptools warns and builds
# metadata with an empty long description.
COPY pyproject.toml README.md ./
# [observability] so init_tracing() in the lifespan can export to Cloud Trace
# instead of always taking its ImportError branch.
RUN pip install --no-cache-dir --prefix=/install -e ".[observability]"

# --- Node.js builder stage ---
# Forge runs the target repo's test suite. Without Node every JS/TS PR's tests
# were reported as skipped, which makes the CI result incomplete, never passed.
FROM node:24.21.0-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553 AS node-builder

RUN npm install -g pnpm@9 && pnpm --version

# --- Runtime stage ---
FROM python:3.14.7-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f

# git: clone the PR branch and fetch its base for the changed-file diff.
# ruff + pytest (from requirements.txt) are the Python CI runners; Node 24 and
# pnpm below run JS/TS suites. A check that still cannot run is reported as
# skipped, which makes the whole CI result incomplete - never passed.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Node binary plus the global node_modules tree; bin entries are symlinks into
# node_modules, so they are recreated rather than copied (a copied dangling
# symlink would break npm/npx/pnpm).
COPY --from=node-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=node-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm     && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx     && ln -sf ../lib/node_modules/pnpm/bin/pnpm.cjs /usr/local/bin/pnpm     && node --version && npm --version && pnpm --version

# Copy Python packages from builder
COPY --from=builder /install /usr/local

RUN groupadd --gid 1001 henchmen && useradd --uid 1001 --gid henchmen --shell /bin/bash --create-home henchmen

WORKDIR /app

COPY src/ ./src/
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --no-deps -e .

RUN chown -R henchmen:henchmen /app

USER henchmen

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status == 200 else 1)"]

CMD ["uvicorn", "henchmen.forge.server:app", "--host", "0.0.0.0", "--port", "8080"]
