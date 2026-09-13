#!/bin/bash
# Dispatch runs the FastAPI intake app as its main process. The Slack Socket
# Mode client is started from the app's lifespan (see
# henchmen.dispatch.server:lifespan) when the Slack tokens are configured, so
# the container stays up and serves /health, /api/v1/tasks, /webhooks/* and
# /pubsub/* whether or not Slack is set up.
set -euo pipefail
export PYTHONUNBUFFERED=1
exec uvicorn henchmen.dispatch.server:app --host 0.0.0.0 --port "${PORT:-8080}"
