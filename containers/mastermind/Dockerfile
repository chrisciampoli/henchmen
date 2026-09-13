# --- Builder stage ---
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY containers/mastermind/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY src/ ./src/
COPY pyproject.toml README.md ./
# [local] so the same image can drive Anthropic/OpenAI operatives and run
# `henchmen serve`; [observability] so init_tracing() and /metrics/prometheus
# do something other than take their ImportError branch.
RUN pip install --no-cache-dir --prefix=/install -e ".[local,observability]"

# --- Node.js builder stage ---
FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS node-builder

# Install pnpm globally and verify
RUN npm install -g pnpm@9 && pnpm --version

# --- Docker CLI stage ---
# Static client binary only (no daemon). Needed in provider=local mode, where
# the scheme executor launches operatives as sibling containers against a
# mounted /var/run/docker.sock.
FROM docker:28-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d AS docker-cli

# --- Runtime stage ---
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Copy Node.js binary + all node_modules (preserves internal structure)
COPY --from=node-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=node-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
# Recreate bin symlinks pointing into the copied node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -sf ../lib/node_modules/pnpm/bin/pnpm.cjs /usr/local/bin/pnpm

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# Verify everything the CI-check handlers shell out to is actually present.
RUN node --version && npm --version && pnpm --version && docker --version

# Copy Python packages from builder
COPY --from=builder /install /usr/local

RUN groupadd --gid 1001 henchmen && useradd --uid 1001 --gid henchmen --shell /bin/bash --create-home henchmen

WORKDIR /app

COPY src/ ./src/
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --no-deps -e .

RUN python -m pytest --version && python -m ruff --version

RUN chown -R henchmen:henchmen /app

USER henchmen

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status == 200 else 1)"]

CMD ["uvicorn", "henchmen.mastermind.server:app", "--host", "0.0.0.0", "--port", "8080"]
