"""CI Runner - executes lint, test and silent-failure checks on a cloned PR branch.

Every check reports one of three statuses:

``passed``
    The check ran and found no problems.
``failed``
    The check ran and found problems, or could not be trusted (fail-closed).
``skipped``
    The check could not run at all (no runner in the image, no test script).
    A skipped check is never reported as a pass: a run with any skipped check
    has ``passed=False`` and ``incomplete=True`` - see :meth:`CIRunner.run`.

All subprocesses run with a wall-clock timeout, the whole run is bounded by a
total budget, the Henchmen secret environment is stripped, and every
credential-bearing string is redacted out of the captured output before it
reaches a log line or a PR comment.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: Default wall-clock budget for the whole CI run (and so for any single
#: subprocess). Forge runs CI synchronously inside the Pub/Sub push request, and
#: the push subscription's ack deadline is 600s (the Pub/Sub maximum): a run that
#: outlives it is redelivered while the first run is still going, producing
#: duplicate CI runs, PR comments and forge-results. 540s leaves headroom for the
#: clone, the GitHub calls and publishing the result.
DEFAULT_CI_TIMEOUT_SECONDS = 540

#: How many commits of the PR base branch to fetch when looking for a merge base.
BASE_FETCH_DEPTH = 200

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_RC_TIMEOUT = -100
_RC_NOT_FOUND = -101

# Git refs are constrained so a task-controlled branch name can never be read as
# an option or a path by the git subprocesses below.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")

# https://user:password@host -> https://***@host
_CREDENTIAL_URL_RE = re.compile(r"(https?://)[^/\s:@]+(?::[^/\s@]*)?@")

_SECRET_ENV_PREFIXES = (
    "HENCHMEN_",
    "GITHUB_",
    "SLACK_",
    "JIRA_",
    "AWS_",
    "GOOGLE_",
    "GCP_",
    "PINECONE_",
    "ANTHROPIC_",
    "OPENAI_",
)
_SECRET_ENV_RE = re.compile(r"TOKEN|SECRET|API_?KEY|PASSWORD|CREDENTIAL", re.IGNORECASE)

_TEST_DIRS = ("tests", "test", "apps/api/test")


def _is_secret_env(name: str) -> bool:
    """True if an environment variable must not be exposed to target-repo code."""
    return name.startswith(_SECRET_ENV_PREFIXES) or bool(_SECRET_ENV_RE.search(name))


class CIRunner:
    """Runs CI checks (lint + tests + silent-failure scan) on a cloned repository."""

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_CI_TIMEOUT_SECONDS,
        total_budget_seconds: int = DEFAULT_CI_TIMEOUT_SECONDS,
        redact: Sequence[str] = (),
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.total_budget_seconds = total_budget_seconds
        self._redact_values = tuple(value for value in redact if value)
        # Monotonic deadline for the current :meth:`run`; ``None`` outside a run.
        self._deadline: float | None = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self, workspace_dir: str, base_ref: str | None = None) -> dict[str, Any]:
        """Run all CI checks against *workspace_dir* and return aggregated results.

        *base_ref* is the PR base branch (``pr.base.ref``). It is required: lint
        and the silent-failure scan are both scoped to the files the PR changed,
        and without a base there is nothing trustworthy to compare against, so
        both checks fail closed rather than silently passing.

        ``passed`` is True only when every check ran and passed. A skipped check
        (no runner in the image, no test script) means the PR was not verified,
        so it makes the run ``incomplete`` and not passed - never a green tick.
        """
        self._deadline = asyncio.get_running_loop().time() + self.total_budget_seconds
        try:
            return await self._run_checks(workspace_dir, base_ref)
        finally:
            self._deadline = None

    async def run_silent_failure_scan(self, workspace_dir: str, base_ref: str | None) -> dict[str, Any]:
        """Run only the silent-failure scan and return its check result.

        Used on a desktop install, where lint and tests run in the gate
        container instead. Nothing here executes target-repo code: it is git
        plumbing on the clone (``fetch``, ``merge-base``, ``diff`` — none of
        which run hooks, filters or diff drivers from a fresh clone's config)
        followed by :class:`~henchmen.forge.silent_failure_detector.SilentFailureDetector`,
        which is pure regex analysis of the diff text. The clone can therefore
        be made with ``--no-checkout``.
        """
        self._deadline = asyncio.get_running_loop().time() + self.total_budget_seconds
        try:
            merge_base, base_error = await self._resolve_merge_base(workspace_dir, base_ref)
            return await self._run_silent_failure_scan(workspace_dir, merge_base, base_error)
        finally:
            self._deadline = None

    async def _run_checks(self, workspace_dir: str, base_ref: str | None) -> dict[str, Any]:
        merge_base, base_error = await self._resolve_merge_base(workspace_dir, base_ref)

        results: list[dict[str, Any]] = [await self._run_lint(workspace_dir, merge_base, base_error)]

        test_result = await self._run_tests(workspace_dir)
        if test_result is not None:
            results.append(test_result)

        results.append(await self._run_silent_failure_scan(workspace_dir, merge_base, base_error))
        return self.aggregate(results)

    def aggregate(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Combine per-check results into the run result :meth:`run` returns.

        ``passed`` only when nothing failed and nothing was skipped;
        ``incomplete`` when something was skipped and nothing failed.
        """
        failed = [r["name"] for r in results if r["status"] == STATUS_FAILED]
        skipped = [r["name"] for r in results if r["status"] == STATUS_SKIPPED]
        return {
            "passed": not failed and not skipped,
            "incomplete": bool(skipped) and not failed,
            "checks": results,
            "failed": failed,
            "skipped": skipped,
            "summary": self._build_summary(results),
        }

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    async def _run_lint(
        self,
        workspace_dir: str,
        merge_base: str | None,
        base_error: str,
    ) -> dict[str, Any]:
        """Run ``ruff check`` over the Python files this PR changed."""
        if merge_base is None:
            return self._check("lint", STATUS_FAILED, "", base_error)

        changed, err = await self._changed_files(workspace_dir, merge_base)
        if err:
            return self._check("lint", STATUS_FAILED, "", err)

        py_files = [f for f in changed if f.endswith(".py")]
        if not py_files:
            return self._check("lint", STATUS_PASSED, "No Python files changed in this PR.", "")

        if not self._module_available("ruff"):
            return self._check("lint", STATUS_SKIPPED, "", "ruff is not installed in the Forge image.")

        rc, out, err = await self._run_command(
            [sys.executable, "-m", "ruff", "check", "--no-cache", *py_files],
            workspace_dir,
        )
        return self._check("lint", STATUS_PASSED if rc == 0 else STATUS_FAILED, out, err)

    async def _run_tests(self, workspace_dir: str) -> dict[str, Any] | None:
        """Run the target repo's test suite, or report why it could not be run."""
        if os.path.exists(os.path.join(workspace_dir, "package.json")):
            return await self._run_node_tests(workspace_dir)

        has_tests = any(os.path.isdir(os.path.join(workspace_dir, d)) for d in _TEST_DIRS)
        if not has_tests:
            return None

        if not self._module_available("pytest"):
            return self._check("tests", STATUS_SKIPPED, "", "pytest is not installed in the Forge image.")

        deps_dir = tempfile.mkdtemp(prefix="forge-deps-")
        try:
            install_note = await self._install_python_dependencies(workspace_dir, deps_dir)
            env = self._child_env()
            env["PYTHONPATH"] = os.pathsep.join([deps_dir, env["PYTHONPATH"]]) if env.get("PYTHONPATH") else deps_dir
            rc, out, err = await self._run_command(
                [sys.executable, "-m", "pytest", "--tb=short", "-q"],
                workspace_dir,
                env=env,
            )
        finally:
            shutil.rmtree(deps_dir, ignore_errors=True)

        status = STATUS_PASSED if rc == 0 else STATUS_FAILED
        return self._check("tests", status, f"{install_note}{out}", err)

    async def _run_node_tests(self, workspace_dir: str) -> dict[str, Any]:
        """Run ``npm test`` for a Node target, or report the check as skipped.

        A Node project whose tests cannot be executed is reported as *skipped*,
        never as a pass - reporting "PASSED" for a suite that never ran is how
        Forge used to rubber-stamp every JS/TS PR.
        """
        script = self._node_test_script(workspace_dir)
        if script is None:
            return self._check("tests", STATUS_SKIPPED, "", "package.json declares no `test` script.")

        npm = shutil.which("npm")
        if npm is None:
            return self._check("tests", STATUS_SKIPPED, "", "npm is not available in the Forge image.")

        install_note = ""
        if not os.path.isdir(os.path.join(workspace_dir, "node_modules")):
            rc, out, err = await self._run_command([npm, "install", "--no-audit", "--no-fund"], workspace_dir)
            if rc != 0:
                return self._check("tests", STATUS_FAILED, out, f"npm install failed: {err}")
            install_note = "npm install completed.\n"

        rc, out, err = await self._run_command([npm, "test"], workspace_dir)
        status = STATUS_PASSED if rc == 0 else STATUS_FAILED
        return self._check("tests", status, f"{install_note}{out}", err)

    async def _run_silent_failure_scan(
        self,
        workspace_dir: str,
        merge_base: str | None,
        base_error: str,
    ) -> dict[str, Any]:
        """Scan the PR diff for silent-failure patterns."""
        from henchmen.forge.silent_failure_detector import SilentFailureDetector

        if merge_base is None:
            return self._check("silent_failure_scan", STATUS_FAILED, "", base_error)

        rc, diff_text, err = await self._run_command(
            ["git", "diff", merge_base, "HEAD"],
            workspace_dir,
        )
        if rc != 0:
            return self._check(
                "silent_failure_scan",
                STATUS_FAILED,
                "",
                f"git diff against the PR base failed: {err or diff_text}",
            )

        if not diff_text.strip():
            return self._check("silent_failure_scan", STATUS_PASSED, "No changes against the PR base.", "")

        detector = SilentFailureDetector()
        findings = detector.scan_diff(diff_text)
        critical_count = sum(1 for f in findings if f.severity == "critical")

        result = self._check(
            "silent_failure_scan",
            STATUS_PASSED if critical_count == 0 else STATUS_FAILED,
            detector.format_findings(findings),
            "",
        )
        result["findings_count"] = len(findings)
        result["critical_count"] = critical_count
        return result

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    async def _resolve_merge_base(self, workspace_dir: str, base_ref: str | None) -> tuple[str | None, str]:
        """Fetch the PR base branch and resolve the merge base with HEAD.

        Returns ``(merge_base_sha, "")`` on success or ``(None, error)`` on
        failure. Forge clones shallow, so the base branch has to be fetched
        explicitly before a merge base exists at all.
        """
        if not base_ref:
            return None, "No PR base ref was supplied, so the changed-file set cannot be determined."
        if not _SAFE_REF_RE.match(base_ref):
            return None, f"Refusing to use an unsafe base ref: {base_ref!r}"

        remote_ref = f"refs/remotes/origin/{base_ref}"
        rc, out, err = await self._run_command(
            [
                "git",
                "fetch",
                f"--depth={BASE_FETCH_DEPTH}",
                "origin",
                f"+refs/heads/{base_ref}:{remote_ref}",
            ],
            workspace_dir,
        )
        if rc != 0:
            return None, f"git fetch of base branch {base_ref!r} failed: {err or out}"

        merge_base, err = await self._merge_base(workspace_dir, f"origin/{base_ref}")
        if merge_base is not None:
            return merge_base, ""

        # Shallow history on either side can hide the fork point - deepen once.
        deepen_rc, deepen_out, deepen_err = await self._run_command(
            ["git", "fetch", f"--deepen={BASE_FETCH_DEPTH}", "origin"],
            workspace_dir,
        )
        if deepen_rc != 0:
            logger.warning("[FORGE] deepen fetch failed: %s", deepen_err or deepen_out)
        else:
            merge_base, err = await self._merge_base(workspace_dir, f"origin/{base_ref}")
            if merge_base is not None:
                return merge_base, ""

        return None, f"Could not find a merge base with {base_ref!r}: {err}"

    async def _merge_base(self, workspace_dir: str, base_rev: str) -> tuple[str | None, str]:
        rc, out, err = await self._run_command(["git", "merge-base", base_rev, "HEAD"], workspace_dir)
        if rc != 0 or not out.strip():
            return None, err or out or "git merge-base returned nothing"
        return out.strip().splitlines()[0], ""

    async def _changed_files(self, workspace_dir: str, merge_base: str) -> tuple[list[str], str]:
        """Return the files this PR added or modified (deletions excluded)."""
        rc, out, err = await self._run_command(
            ["git", "diff", "--name-only", "--diff-filter=d", merge_base, "HEAD"],
            workspace_dir,
        )
        if rc != 0:
            return [], f"git diff --name-only against the PR base failed: {err or out}"
        # A path can never legitimately start with '-'; dropping such entries keeps
        # a hostile filename from being parsed as an option by the lint command.
        return [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("-")], ""

    # ------------------------------------------------------------------
    # Dependency installation
    # ------------------------------------------------------------------

    async def _install_python_dependencies(self, workspace_dir: str, deps_dir: str) -> str:
        """Best-effort install of the target repo's dependencies into *deps_dir*.

        Installs into a throwaway directory rather than the Forge image's own
        site-packages so a target repo cannot replace Forge's ruff/pytest.
        The install runs target-controlled build code, exactly as running the
        target's test suite does, so it uses the same scrubbed environment.
        """
        if os.path.exists(os.path.join(workspace_dir, "requirements.txt")):
            target = ["-r", "requirements.txt"]
        elif os.path.exists(os.path.join(workspace_dir, "pyproject.toml")) or os.path.exists(
            os.path.join(workspace_dir, "setup.py")
        ):
            target = ["."]
        else:
            return ""

        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-input",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--target",
            deps_dir,
            *target,
        ]
        rc, out, err = await self._run_command(cmd, workspace_dir)
        if rc == 0:
            return "Installed target dependencies.\n"
        logger.warning("[FORGE] Dependency install failed (continuing): %s", (err or out)[:500])
        return f"Dependency install failed (tests may fail on imports):\n{(err or out)[:1000]}\n"

    # ------------------------------------------------------------------
    # Process helpers
    # ------------------------------------------------------------------

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str,
        *,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run *cmd* under ``self.timeout_seconds``. Returns ``(returncode, stdout, stderr)``.

        Never raises: a missing executable and a timeout both come back as a
        distinct non-zero return code with an explanatory stderr, so callers
        fail closed instead of mistaking them for success.
        """
        budget: float = self.timeout_seconds
        if self._deadline is not None:
            remaining = self._deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return _RC_TIMEOUT, "", f"CI time budget of {self.total_budget_seconds}s exhausted before {cmd[0]} ran"
            budget = min(budget, remaining)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env or self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            return _RC_NOT_FOUND, "", f"Could not run {cmd[0]!r}: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=budget)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return _RC_TIMEOUT, "", f"Command timed out after {budget:.0f}s:{cmd[0]} {' '.join(cmd[1:3])}"

        return (
            proc.returncode if proc.returncode is not None else _RC_NOT_FOUND,
            self._sanitize(stdout.decode("utf-8", errors="replace")),
            self._sanitize(stderr.decode("utf-8", errors="replace")),
        )

    def _child_env(self) -> dict[str, str]:
        """Environment for CI subprocesses, with Henchmen's secrets stripped.

        CI runs target-repo code (conftest.py, build backends, npm scripts).
        That code must never see the GitHub token or any provider credential
        that happens to be in the Forge service's environment.
        """
        env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        return env

    def _sanitize(self, text: str) -> str:
        """Redact credentials from output that is logged and posted to the PR."""
        for secret in self._redact_values:
            text = text.replace(secret, "***")
        return _CREDENTIAL_URL_RE.sub(r"\1***@", text)

    @staticmethod
    def _module_available(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):  # pragma: no cover - defensive
            return False

    @staticmethod
    def _node_test_script(workspace_dir: str) -> str | None:
        """Return the target's npm ``test`` script, or ``None`` if it has none."""
        try:
            with open(os.path.join(workspace_dir, "package.json"), encoding="utf-8") as fh:
                package = json.load(fh)
        except (OSError, ValueError):
            return None
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if not isinstance(scripts, dict):
            return None
        script = scripts.get("test")
        return script if isinstance(script, str) and script.strip() else None

    # ------------------------------------------------------------------
    # Result shaping
    # ------------------------------------------------------------------

    @staticmethod
    def check_result(name: str, status: str, output: str, error: str) -> dict[str, Any]:
        """One check's result dict, with output and error truncated for the PR comment."""
        return CIRunner._check(name, status, output, error)

    @staticmethod
    def _check(name: str, status: str, output: str, error: str) -> dict[str, Any]:
        return {
            "name": name,
            "status": status,
            "passed": status == STATUS_PASSED,
            "output": output[:5000],
            "error": error[:2000],
        }

    def _build_summary(self, results: list[dict[str, Any]]) -> str:
        """Build a human-readable summary of all check results."""
        label = {STATUS_PASSED: "PASS", STATUS_FAILED: "FAIL", STATUS_SKIPPED: "SKIP"}
        lines: list[str] = []
        for r in results:
            lines.append(f"{label.get(r['status'], 'UNKNOWN')}: {r['name']}")
            detail = r.get("error") or (r.get("output") if r["status"] != STATUS_PASSED else "")
            if detail:
                lines.append(f"  {detail[:500]}")
        return "\n".join(lines)
