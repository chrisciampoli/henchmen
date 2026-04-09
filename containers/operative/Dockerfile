# --- Builder stage ---
FROM python:3.12.8-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY containers/operative/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src/ ./src/
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install -e .

# --- Node.js builder stage ---
FROM node:20.18-slim AS node-builder

# Install pnpm globally and verify
RUN npm install -g pnpm@9 && pnpm --version
# Find the actual pnpm entrypoint for copying
RUN which pnpm && ls -la $(which pnpm) && readlink -f $(which pnpm)

# --- Runtime stage ---
FROM python:3.12.8-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Copy entire /usr/local from node-builder (preserves all symlinks, modules, bin structure)
# This is heavy but correct — avoids symlink breakage from selective COPY
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
RUN pip install --no-cache-dir -e .

RUN mkdir -p /workspace && chown -R henchmen:henchmen /app /workspace

USER henchmen

# Operative is a Cloud Run Job (no EXPOSE, no HEALTHCHECK)
ENTRYPOINT ["python", "-m", "henchmen.operative.bootstrap"]
