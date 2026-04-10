"""`henchmen doctor` — self-check CLI command.

Runs a series of diagnostic checks to verify that the local environment
is ready to run Henchmen. Exits non-zero if any check fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CheckStatus(str, Enum):  # noqa: UP042 — project convention: str, Enum pattern per CLAUDE.md
    """Outcome of a single diagnostic check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    """Result of a single check."""

    name: str
    status: CheckStatus
    message: str
    hint: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == CheckStatus.OK

    @property
    def is_failure(self) -> bool:
        return self.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    """Verify Python >= 3.12."""
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        return CheckResult(
            name="Python version",
            status=CheckStatus.OK,
            message=f"Python {major}.{minor} detected",
        )
    return CheckResult(
        name="Python version",
        status=CheckStatus.FAIL,
        message=f"Henchmen requires Python >= 3.12; found {major}.{minor}",
        hint="Install Python 3.12 from https://www.python.org/downloads/",
    )


def check_docker() -> CheckResult:
    """Verify Docker is installed and the daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return CheckResult(
            name="Docker",
            status=CheckStatus.FAIL,
            message="Docker CLI not found on PATH",
            hint="Install Docker Desktop from https://docs.docker.com/get-docker/",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="Docker",
            status=CheckStatus.FAIL,
            message="docker info timed out after 10s",
            hint="Is the Docker daemon running? Check with `docker ps`.",
        )

    if result.returncode != 0:
        err = (result.stderr or "").strip().splitlines()[0] if result.stderr else "unknown error"
        return CheckResult(
            name="Docker",
            status=CheckStatus.FAIL,
            message=f"Docker CLI present but daemon unreachable: {err}",
            hint="Start Docker Desktop or the docker service, then rerun `henchmen doctor`.",
        )

    # Extract the server version line for a friendly OK message.
    version = "running"
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("Server Version"):
            version = line.split(":", 1)[1].strip()
            break
    return CheckResult(
        name="Docker",
        status=CheckStatus.OK,
        message=f"Docker daemon reachable (version {version})",
    )


def check_git_identity() -> CheckResult:
    """Verify ``git config user.name`` and ``user.email`` are set."""
    try:
        name_result = subprocess.run(
            ["git", "config", "--get", "user.name"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        email_result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return CheckResult(
            name="Git identity",
            status=CheckStatus.FAIL,
            message="git CLI not found on PATH",
            hint="Install git from https://git-scm.com/",
        )

    name = (name_result.stdout or "").strip()
    email = (email_result.stdout or "").strip()
    if name_result.returncode != 0 or email_result.returncode != 0 or not name or not email:
        return CheckResult(
            name="Git identity",
            status=CheckStatus.FAIL,
            message="git user.name or user.email is not configured",
            hint="Set both:\n  git config --global user.name 'Your Name'\n  git config --global user.email 'you@example.com'",
        )
    return CheckResult(
        name="Git identity",
        status=CheckStatus.OK,
        message=f"{name} <{email}>",
    )


def check_env_file() -> CheckResult:
    """Check that a ``.env.local`` (or ``.env`` / ``.env.example``) is discoverable."""
    cwd = Path.cwd()
    if (cwd / ".env.local").is_file():
        return CheckResult(
            name=".env.local",
            status=CheckStatus.OK,
            message="Found .env.local in current directory",
        )
    if (cwd / ".env").is_file():
        return CheckResult(
            name=".env.local",
            status=CheckStatus.WARN,
            message="No .env.local — using .env as fallback",
            hint="Copy .env.example to .env.local and customize it: `cp .env.example .env.local`",
        )
    if (cwd / ".env.example").is_file():
        return CheckResult(
            name=".env.local",
            status=CheckStatus.WARN,
            message=".env.example found but .env.local missing",
            hint="Copy .env.example to .env.local and customize it: `cp .env.example .env.local`",
        )
    return CheckResult(
        name=".env.local",
        status=CheckStatus.WARN,
        message="No .env / .env.local / .env.example in current directory",
        hint="Create a .env.local with `HENCHMEN_` env vars — see docs/deploy-gcp.md.",
    )


def check_llm_credentials() -> CheckResult:
    """Verify credentials are present for the configured LLM provider."""
    provider = os.environ.get("HENCHMEN_LLM_PROVIDER", "").strip().lower()
    if not provider:
        provider = os.environ.get("HENCHMEN_PROVIDER", "local").strip().lower()

    if provider in ("ollama", "local", ""):
        return CheckResult(
            name="LLM credentials",
            status=CheckStatus.OK,
            message="Provider=ollama (no API key required)",
        )
    if provider == "openai":
        key = os.environ.get("HENCHMEN_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if key:
            return CheckResult(
                name="LLM credentials",
                status=CheckStatus.OK,
                message="HENCHMEN_OPENAI_API_KEY is set",
            )
        return CheckResult(
            name="LLM credentials",
            status=CheckStatus.FAIL,
            message="HENCHMEN_LLM_PROVIDER=openai but no API key set",
            hint="Set HENCHMEN_OPENAI_API_KEY in .env.local",
        )
    if provider == "anthropic":
        key = os.environ.get("HENCHMEN_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return CheckResult(
                name="LLM credentials",
                status=CheckStatus.OK,
                message="HENCHMEN_ANTHROPIC_API_KEY is set",
            )
        return CheckResult(
            name="LLM credentials",
            status=CheckStatus.FAIL,
            message="HENCHMEN_LLM_PROVIDER=anthropic but no API key set",
            hint="Set HENCHMEN_ANTHROPIC_API_KEY in .env.local",
        )
    if provider in ("gcp", "vertex"):
        # Vertex AI uses Application Default Credentials
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_CLOUD_PROJECT"):
            return CheckResult(
                name="LLM credentials",
                status=CheckStatus.OK,
                message="GCP ADC environment detected",
            )
        return CheckResult(
            name="LLM credentials",
            status=CheckStatus.WARN,
            message="Provider=gcp but GOOGLE_APPLICATION_CREDENTIALS not set",
            hint="Run `gcloud auth application-default login` before running henchmen.",
        )
    if provider == "aws":
        return CheckResult(
            name="LLM credentials",
            status=CheckStatus.WARN,
            message="Provider=aws — AWS support is experimental",
            hint="Configure an AWS profile with Bedrock InvokeModel permissions.",
        )

    return CheckResult(
        name="LLM credentials",
        status=CheckStatus.WARN,
        message=f"Unknown provider {provider!r} — cannot verify credentials",
    )


def check_operative_image() -> CheckResult:
    """Check whether the local operative Docker image has been built."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "henchmen-operative:local"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CheckResult(
            name="Operative image",
            status=CheckStatus.WARN,
            message="Cannot inspect image (docker not available)",
        )
    if result.returncode == 0:
        return CheckResult(
            name="Operative image",
            status=CheckStatus.OK,
            message="henchmen-operative:local exists",
        )
    return CheckResult(
        name="Operative image",
        status=CheckStatus.WARN,
        message="henchmen-operative:local not built yet",
        hint="Run `henchmen build-operative` to build it (~3 min on first run).",
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_doctor() -> list[CheckResult]:
    """Run every registered check and return the list of results."""
    return [
        check_python_version(),
        check_docker(),
        check_git_identity(),
        check_env_file(),
        check_llm_credentials(),
        check_operative_image(),
    ]


def _format_result(result: CheckResult) -> str:
    """Format a single CheckResult for stdout."""
    glyphs = {
        CheckStatus.OK: "[OK]",
        CheckStatus.WARN: "[WARN]",
        CheckStatus.FAIL: "[FAIL]",
    }
    glyph = glyphs[result.status]
    out = f"  {glyph:6s} {result.name}: {result.message}"
    if result.hint:
        hint_lines = result.hint.splitlines()
        for hint_line in hint_lines:
            out += f"\n         ↳ {hint_line}"
    return out


def run_doctor_cli() -> int:
    """Run all checks and print a formatted report. Returns exit code."""
    results = run_doctor()

    print("henchmen doctor — self-check")
    print()
    for r in results:
        print(_format_result(r))
    print()

    failures = sum(1 for r in results if r.is_failure)
    warnings = sum(1 for r in results if r.status == CheckStatus.WARN)
    oks = sum(1 for r in results if r.is_ok)

    print(f"Result: {oks} ok, {warnings} warnings, {failures} failures")
    return 0 if failures == 0 else 1
