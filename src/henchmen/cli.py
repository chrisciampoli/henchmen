"""Henchmen CLI — single-process server for local development."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn


def main() -> None:
    """Entry point for the henchmen CLI."""
    parser = argparse.ArgumentParser(description="Henchmen AI Agent Factory")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run all services in a single process (local dev)")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    serve_parser.add_argument("--log-level", default="info", help="Log level")

    args = parser.parse_args()

    if args.command == "serve":
        _serve(args)
    else:
        parser.print_help()
        sys.exit(1)


def _serve(args: argparse.Namespace) -> None:
    """Run Dispatch + Mastermind + Forge in a single process."""
    os.environ.setdefault("HENCHMEN_PROVIDER", "local")
    os.environ.setdefault("HENCHMEN_ENVIRONMENT", "dev")
    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "local-dev")

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    logger = logging.getLogger("henchmen")
    logger.info("Starting Henchmen in single-process mode (provider=local)")

    from fastapi import FastAPI

    app = FastAPI(title="Henchmen (Local Dev)", version="0.1.0")

    from henchmen.dispatch.server import app as dispatch_app
    from henchmen.forge.server import app as forge_app
    from henchmen.mastermind.server import app as mastermind_app

    app.mount("/dispatch", dispatch_app)
    app.mount("/mastermind", mastermind_app)
    app.mount("/forge", forge_app)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "mode": "local", "services": ["dispatch", "mastermind", "forge"]}

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
