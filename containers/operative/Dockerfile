# --- Builder stage ---
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY containers/operative/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src/ ./src/
COPY pyproject.toml .
# [local] brings the openai + anthropic SDKs so direct-LLM operatives work.
# pytest/ruff/mypy are the tools the agent runs against a Python target repo.
RUN pip install --no-cache-dir --prefix=/install -e ".[local]" \
    && pip install --no-cache-dir --prefix=/install pytest ruff mypy

# --- Node.js builder stage ---
FROM node:20.18-slim@sha256:ffc11dbf16dd0abcbb7b837410601b4d5592db2d03741e13a4a5336ab74d7ccb AS node-builder

# Install pnpm globally and verify
RUN npm install -g pnpm@9 && pnpm --version

# --- Runtime stage ---
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Selective copy: the node binary plus the global node_modules tree. The bin
# entries are symlinks into node_modules, so they are recreated below rather
# than copied (a copied dangling symlink would break npm/npx/pnpm).
COPY --from=node-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=node-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
# Recreate bin symlinks pointing into the copied node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -sf ../lib/node_modules/pnpm/bin/pnpm.cjs /usr/local/bin/pnpm

# Verify everything works
RUN node --version && npm --version && pnpm --version

# Copy Python packages from builder
COPY --from=builder /install /usr/local

RUN groupadd --gid 1001 henchmen && useradd --uid 1001 --gid henchmen --shell /bin/bash --create-home henchmen

WORKDIR /app

COPY src/ ./src/
COPY pyproject.toml .
RUN pip install --no-cache-dir --no-deps -e .

RUN mkdir -p /workspace && chown -R henchmen:henchmen /app /workspace

USER henchmen

# Operative is a Cloud Run Job (no EXPOSE, no HEALTHCHECK)
ENTRYPOINT ["python", "-m", "henchmen.operative.bootstrap"]
