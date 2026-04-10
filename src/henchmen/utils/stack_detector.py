"""Language stack detection for operative workspaces.

Detects the project's primary stack by inspecting well-known manifest
files (``pyproject.toml``, ``package.json``, ``go.mod``, etc.) and
returns a :class:`Stack` describing which commands the CI handlers
should run to lint and test the changed code.

Removes the old JS/TS-only hard-coding from
``src/henchmen/mastermind/scheme_executor/handlers.py`` — Henchmen now
works against Python, Node (pnpm/npm), Go, Rust, and Java repositories
out of the box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Stack:
    """Describes how to install, lint, and test a project.

    :attr name: Canonical stack identifier (``"python"``, ``"node-pnpm"``,
        ``"node-npm"``, ``"go"``, ``"rust"``, ``"java-maven"``,
        ``"java-gradle"``, or ``"unknown"``).
    :attr test_command: Argv list used to run the test suite.
    :attr lint_command: Argv list used to run the linter.
    :attr install_command: Optional argv list used to install dependencies
        before lint/test. ``None`` when the stack has no install step
        (e.g. Go, Rust, Java — the toolchain downloads dependencies on
        first invocation).
    :attr is_monorepo: True when the stack looks like a monorepo (e.g.
        pnpm + turbo). Used by the scheme executor to scope lint/tests
        to affected packages.
    """

    name: str
    test_command: list[str] = field(default_factory=list)
    lint_command: list[str] = field(default_factory=list)
    install_command: list[str] | None = None
    is_monorepo: bool = False

    def __hash__(self) -> int:  # dataclass with mutable default — make hashable explicitly
        return hash(
            (
                self.name,
                tuple(self.test_command),
                tuple(self.lint_command),
                tuple(self.install_command) if self.install_command else None,
                self.is_monorepo,
            )
        )


# ---------------------------------------------------------------------------
# Stack definitions
# ---------------------------------------------------------------------------


def _python_stack() -> Stack:
    return Stack(
        name="python",
        test_command=["python", "-m", "pytest", "--no-header", "-q"],
        lint_command=["python", "-m", "ruff", "check", "."],
        install_command=None,
    )


def _node_pnpm_stack(is_monorepo: bool) -> Stack:
    return Stack(
        name="node-pnpm",
        test_command=["pnpm", "run", "test"],
        lint_command=["pnpm", "run", "lint"],
        install_command=["pnpm", "install", "--frozen-lockfile"],
        is_monorepo=is_monorepo,
    )


def _node_npm_stack() -> Stack:
    return Stack(
        name="node-npm",
        test_command=["npm", "test", "--", "--passWithNoTests"],
        lint_command=["npm", "run", "lint"],
        install_command=["npm", "ci"],
    )


def _go_stack() -> Stack:
    return Stack(
        name="go",
        test_command=["go", "test", "./..."],
        lint_command=["go", "vet", "./..."],
        install_command=None,
    )


def _rust_stack() -> Stack:
    return Stack(
        name="rust",
        test_command=["cargo", "test"],
        lint_command=["cargo", "clippy", "--", "-D", "warnings"],
        install_command=None,
    )


def _java_maven_stack() -> Stack:
    return Stack(
        name="java-maven",
        test_command=["mvn", "test"],
        lint_command=["mvn", "verify", "-DskipTests"],
        install_command=None,
    )


def _java_gradle_stack() -> Stack:
    return Stack(
        name="java-gradle",
        test_command=["./gradlew", "test"],
        lint_command=["./gradlew", "check", "-x", "test"],
        install_command=None,
    )


def _unknown_stack() -> Stack:
    return Stack(name="unknown")


# ---------------------------------------------------------------------------
# detect_stack
# ---------------------------------------------------------------------------


def detect_stack(workspace: Path) -> Stack:
    """Return the :class:`Stack` describing the project at ``workspace``.

    Detection order (first match wins):

    1. Python — ``pyproject.toml``, ``setup.py``, or ``requirements.txt``
       Python wins over Node when both signatures are present because a
       pyproject.toml is authoritative for Python projects while a
       package.json is often added just for dev tooling.
    2. Rust — ``Cargo.toml``
    3. Go — ``go.mod``
    4. Java (Maven) — ``pom.xml``
    5. Java (Gradle) — ``build.gradle`` or ``build.gradle.kts``
    6. Node (pnpm) — ``package.json`` + ``pnpm-lock.yaml`` (monorepo when
       ``turbo.json`` is also present)
    7. Node (npm) — ``package.json`` (any lockfile or none)
    8. Otherwise ``unknown``
    """
    workspace = Path(workspace)

    # 1. Python
    if (
        (workspace / "pyproject.toml").exists()
        or (workspace / "setup.py").exists()
        or (workspace / "requirements.txt").exists()
    ):
        return _python_stack()

    # 2. Rust
    if (workspace / "Cargo.toml").exists():
        return _rust_stack()

    # 3. Go
    if (workspace / "go.mod").exists():
        return _go_stack()

    # 4. Java (Maven)
    if (workspace / "pom.xml").exists():
        return _java_maven_stack()

    # 5. Java (Gradle)
    if (workspace / "build.gradle").exists() or (workspace / "build.gradle.kts").exists():
        return _java_gradle_stack()

    # 6/7. Node
    if (workspace / "package.json").exists():
        if (workspace / "pnpm-lock.yaml").exists():
            is_monorepo = (workspace / "turbo.json").exists()
            return _node_pnpm_stack(is_monorepo=is_monorepo)
        return _node_npm_stack()

    return _unknown_stack()
