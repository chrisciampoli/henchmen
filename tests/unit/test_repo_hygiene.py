"""Regression tests for repo packaging, container and CI configuration.

Every assertion here pins a defect that shipped at least once: a fail-open CI
smoke test, a secret scanner with zero rules, a pre-commit hook that could not
parse pyproject, stale ruff ignores, a hard-coded ``__version__`` that drifted
from pyproject, and a docker-compose stack that could not route a task.
"""

from __future__ import annotations

import subprocess
import tomllib
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

import pytest
import yaml

import henchmen

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINERS_DIR = REPO_ROOT / "containers"


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _load_yaml(relative: str) -> dict[str, Any]:
    data = yaml.safe_load(_read(relative))
    assert isinstance(data, dict), f"{relative} did not parse to a mapping"
    return data


def _load_pyproject() -> dict[str, Any]:
    return tomllib.loads(_read("pyproject.toml"))


def _extras() -> dict[str, list[str]]:
    return _load_pyproject()["project"]["optional-dependencies"]


# --------------------------------------------------------------------------
# Repo hygiene
# --------------------------------------------------------------------------


def test_local_sqlite_store_is_not_tracked_by_git() -> None:
    """`henchmen_{env}.db` is runtime state — it must never be versioned.

    It was committed together with its WAL, which carried task descriptions
    and a PR URL from local runs into a public repo, and dirtied the tree on
    every `henchmen serve`.
    """
    result = subprocess.run(
        ["git", "ls-files", "--", "henchmen_*.db", "henchmen_*.db-shm", "henchmen_*.db-wal"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    assert result.stdout.strip() == "", f"local SQLite state is tracked: {result.stdout.strip()}"


@pytest.mark.parametrize(
    "pattern",
    ["henchmen_*.db", "henchmen_*.db-shm", "henchmen_*.db-wal", ".superpowers/", ".henchmen/"],
)
def test_gitignore_covers_local_runtime_state(pattern: str) -> None:
    lines = {line.strip() for line in _read(".gitignore").splitlines()}
    assert pattern in lines


def test_dockerignore_keeps_the_declared_readme() -> None:
    """pyproject declares readme = README.md; excluding it breaks wheel metadata."""
    assert _load_pyproject()["project"]["readme"] == "README.md"
    lines = [line.strip() for line in _read(".dockerignore").splitlines()]
    assert "*.md" in lines
    assert "!README.md" in lines
    assert lines.index("!README.md") > lines.index("*.md"), "the negation must come after the exclusion"


# --------------------------------------------------------------------------
# Secret scanning
# --------------------------------------------------------------------------


def test_gitleaks_config_extends_the_default_ruleset() -> None:
    """Without [extend] a repo config REPLACES the built-in rules with nothing."""
    config = tomllib.loads(_read(".gitleaks.toml"))
    assert config.get("extend", {}).get("useDefault") is True


def test_ci_runs_a_secret_scan() -> None:
    """The pre-commit hook is optional; fork PRs must still be scanned."""
    ci = _load_yaml(".github/workflows/ci.yml")
    assert "secrets" in ci["jobs"]
    steps = yaml.dump(ci["jobs"]["secrets"])
    assert "gitleaks" in steps


def test_ci_and_precommit_agree_on_the_gitleaks_version() -> None:
    precommit = _load_yaml(".pre-commit-config.yaml")
    hook_rev = next(repo["rev"] for repo in precommit["repos"] if repo["repo"].endswith("/gitleaks"))
    assert f"gitleaks:{hook_rev}" in _read(".github/workflows/ci.yml")


# --------------------------------------------------------------------------
# Lint / type tooling
# --------------------------------------------------------------------------


def test_precommit_ruff_rev_satisfies_the_dev_extra_pin() -> None:
    """A hook older than the pyproject ruleset errors on every commit.

    ruff 0.8.6 could not parse `ASYNC240` at all, so `pre-commit install`
    blocked every commit with a TOML parse error.
    """
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    precommit = _load_yaml(".pre-commit-config.yaml")
    hook_rev = next(repo["rev"] for repo in precommit["repos"] if repo["repo"].endswith("/ruff-pre-commit"))
    ruff_requirement = next(dep for dep in _extras()["dev"] if dep.startswith("ruff"))
    specifier = SpecifierSet(ruff_requirement.removeprefix("ruff"))
    assert Version(hook_rev.lstrip("v")) in specifier, (
        f"pre-commit pins ruff {hook_rev} but pyproject requires {ruff_requirement}"
    )


def test_per_file_ignores_point_at_files_that_exist() -> None:
    """A per-file-ignore for a deleted module silently protects nothing."""
    ignores = _load_pyproject()["tool"]["ruff"]["lint"]["per-file-ignores"]
    missing = [pattern for pattern in ignores if not list(REPO_ROOT.glob(pattern))]
    assert missing == [], f"ruff per-file-ignores reference paths that do not exist: {missing}"


def test_ruff_and_mypy_cover_the_eval_harness_directory() -> None:
    ci = _read(".github/workflows/ci.yml")
    assert "ruff check src/ tests/ evals/" in ci
    assert "ruff format --check src/ tests/ evals/" in ci
    assert "mypy src/ evals/" in ci


def test_mypy_excludes_the_deliberately_broken_fixture_repos() -> None:
    assert "^evals/fixtures/" in _load_pyproject()["tool"]["mypy"]["exclude"]


# --------------------------------------------------------------------------
# Packaging
# --------------------------------------------------------------------------


def test_version_is_read_from_distribution_metadata() -> None:
    """pyproject said 0.2.1 while henchmen.__version__ said 0.1.0."""
    assert henchmen.__version__ == distribution_version("henchmen")
    source = _read("src/henchmen/__init__.py")
    assert 'version("henchmen")' in source, "version must be read back, not hard-coded"


def test_local_extra_covers_everything_a_self_hoster_needs() -> None:
    """`pip install -e .[local]` must be enough to run and evaluate locally."""
    local = _extras()["local"]
    assert local == ["henchmen[openai,anthropic,slack,evals]"]
    assert any(dep.startswith("aiosqlite") for dep in _extras()["evals"])


def test_all_extra_includes_the_local_stack() -> None:
    """dev-integration -> all; the unit suite imports henchmen.evals.storage."""
    assert "local" in _extras()["all"][0]


def test_aws_extra_declares_pyyaml() -> None:
    """providers/aws/codebuild.py imports yaml; boto3 does not provide it."""
    assert any(dep.startswith("pyyaml") for dep in _extras()["aws"])


@pytest.mark.parametrize("unused", ["google-adk", "google-cloud-secret-manager", "google-cloud-logging"])
def test_gcp_extra_drops_sdks_nothing_imports(unused: str) -> None:
    assert unused not in _extras()["gcp"]


def test_python_classifiers_match_what_ci_runs() -> None:
    """A 3.13 classifier advertised support nothing built or tested."""
    classifiers = _load_pyproject()["project"]["classifiers"]
    declared = {
        c.rsplit("::", 1)[1].strip() for c in classifiers if c.startswith("Programming Language :: Python :: 3.")
    }
    tested = set()
    for job in _load_yaml(".github/workflows/ci.yml")["jobs"].values():
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        for step in job.get("steps", []):
            python_version = step.get("with", {}).get("python-version")
            if not python_version:
                continue
            python_version = str(python_version)
            if python_version.replace(" ", "") == "${{matrix.python-version}}":
                # Expand a matrix reference into the versions it actually runs.
                tested.update(str(v) for v in matrix.get("python-version", []))
            else:
                tested.add(python_version)
    assert declared <= tested, f"classifiers claim {declared - tested} but CI never runs it"


# --------------------------------------------------------------------------
# docker-compose topology
# --------------------------------------------------------------------------


def test_compose_runs_one_process_so_the_broker_is_shared() -> None:
    """Three containers meant three InMemoryMessageBrokers and no routing."""
    services = _load_yaml("docker-compose.yml")["services"]
    assert set(services) == {"ollama", "henchmen"}
    assert "serve" in services["henchmen"]["command"]


def test_compose_mounts_the_docker_socket_for_operative_dispatch() -> None:
    henchmen_service = _load_yaml("docker-compose.yml")["services"]["henchmen"]
    assert any("/var/run/docker.sock" in volume for volume in henchmen_service["volumes"])
    assert henchmen_service["environment"]["HENCHMEN_LOCAL_FORWARD_BASE_URL"].startswith("http://host.docker.internal")


def test_every_compose_service_declares_a_healthcheck() -> None:
    """The CI smoke gate requires every service to report healthy."""
    services = _load_yaml("docker-compose.yml")["services"]
    assert all("healthcheck" in svc for svc in services.values()), services.keys()


def test_ci_ollama_stub_tag_matches_the_compose_image() -> None:
    """A drifted tag makes compose pull the real 2GB image in CI."""
    compose_image = _load_yaml("docker-compose.yml")["services"]["ollama"]["image"]
    assert f"docker build -t {compose_image} ci/stub-ollama" in _read(".github/workflows/ci.yml")


# --------------------------------------------------------------------------
# CI / release workflows
# --------------------------------------------------------------------------


def test_compose_smoke_gate_is_fail_closed() -> None:
    """`docker compose ps` hides exited containers and the old grep passed on
    any single 'healthy' line, so the job was green with a dead service."""
    ci = _read(".github/workflows/ci.yml")
    assert "docker compose ps -a" in ci
    assert "docker compose config --services" in ci
    assert "docker compose ps --format" not in ci


@pytest.mark.parametrize("service", sorted(p.name for p in CONTAINERS_DIR.iterdir() if p.is_dir()))
def test_ci_builds_every_container_image(service: str) -> None:
    """The operative image was previously first built at release time."""
    matrix = _load_yaml(".github/workflows/ci.yml")["jobs"]["docker-build"]["strategy"]["matrix"]
    assert service in matrix["service"]


def test_release_is_gated_on_lint_type_and_tests() -> None:
    jobs = _load_yaml(".github/workflows/release.yml")["jobs"]
    assert "verify" in jobs["images"]["needs"]
    assert "verify" in jobs["publish"]["needs"]


def test_release_publishes_only_after_images_are_pushed() -> None:
    """A failed push used to leave a release advertising missing images."""
    jobs = _load_yaml(".github/workflows/release.yml")["jobs"]
    assert "images" in jobs["publish"]["needs"]


def test_release_lowercases_the_ghcr_repository() -> None:
    """GHCR rejects uppercase paths; github.repository keeps the owner's case."""
    assert "tr '[:upper:]' '[:lower:]'" in _read(".github/workflows/release.yml")


def test_release_checks_the_tag_against_the_packaged_version() -> None:
    assert "does not match packaged version" in _read(".github/workflows/release.yml")


def test_evals_workflow_uses_the_eval_run_subcommand() -> None:
    evals = _read(".github/workflows/evals.yml")
    assert "henchmen eval run --provider" in evals
    assert "henchmen eval --provider" not in evals
    assert " --all" not in evals


def test_evals_workflow_installs_extras_for_every_offered_provider() -> None:
    """`local` lacks google-genai, so the vertex choice ImportErrors."""
    workflow = _load_yaml(".github/workflows/evals.yml")
    # YAML 1.1 parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    options = triggers["workflow_dispatch"]["inputs"]["provider"]["options"]
    assert "ollama" not in options, "a hosted runner has no Ollama server"
    assert 'pip install -e ".[all,dev]"' in _read(".github/workflows/evals.yml")


# --------------------------------------------------------------------------
# Mastermind image
# --------------------------------------------------------------------------


def _mastermind_dockerfile() -> str:
    return _read("containers/mastermind/Dockerfile")


@pytest.mark.parametrize("tool", ["pytest", "ruff"])
def test_mastermind_image_ships_the_python_ci_toolchain(tool: str) -> None:
    """In gcp/aws mode run_lint / run_tests exec `python -m <tool>` on this host."""
    requirements = _read("containers/mastermind/requirements.txt")
    assert any(line.startswith(tool) for line in requirements.splitlines())
    assert f"python -m {tool} --version" in _mastermind_dockerfile()


def test_mastermind_image_ships_a_docker_client() -> None:
    """provider=local launches operatives as sibling containers."""
    assert "/usr/local/bin/docker" in _mastermind_dockerfile()


def test_mastermind_image_installs_the_local_and_observability_extras() -> None:
    assert '-e ".[local,observability]"' in _mastermind_dockerfile()


def test_mastermind_image_copies_the_readme_pyproject_declares() -> None:
    assert "COPY pyproject.toml README.md ./" in _mastermind_dockerfile()


@pytest.mark.parametrize("stale", ["node:20", "python:3.12.8", "google-adk"])
def test_mastermind_image_has_no_stale_pins(stale: str) -> None:
    assert stale not in _mastermind_dockerfile()
    assert stale not in _read("containers/mastermind/requirements.txt")


# --------------------------------------------------------------------------
# Every service image
# --------------------------------------------------------------------------

SERVICES = sorted(p.name for p in CONTAINERS_DIR.iterdir() if p.is_dir())


def _dockerfile(service: str) -> str:
    return _read(f"containers/{service}/Dockerfile")


def _external_from_lines(text: str) -> list[str]:
    """FROM lines whose base is an external image, not an earlier stage name.

    A multi-stage Dockerfile may do ``FROM runtime AS local`` where ``runtime``
    is a stage declared earlier in the same file, not a pullable image — that
    line can never carry a ``@sha256:`` digest and must be exempt. Anything
    that isn't a previously declared stage name is a real external image and
    still needs one.
    """
    declared_stages: set[str] = set()
    external: list[str] = []
    for line in text.splitlines():
        if not line.startswith("FROM "):
            continue
        parts = line.split()
        base = parts[1]
        if base not in declared_stages:
            external.append(line)
        if len(parts) >= 4 and parts[2].upper() == "AS":
            declared_stages.add(parts[3])
    return external


@pytest.mark.parametrize("service", SERVICES)
def test_every_pinned_base_image_carries_a_digest(service: str) -> None:
    """Tag-only pins let a rebuilt upstream tag change the image silently.

    ``FROM <earlier-stage> AS <name>`` lines are exempt: the base names a
    stage declared earlier in the same Dockerfile, not a pullable image, so
    it cannot carry a digest.
    """
    for line in _external_from_lines(_dockerfile(service)):
        assert "@sha256:" in line, line


def test_undigested_external_image_in_stage_position_still_fails() -> None:
    """Guard against the exemption swallowing real external images too.

    A stage that pulls a fresh external image (not a prior stage) must still
    be caught even when it appears after other named stages.
    """
    dockerfile = (
        "FROM python:3.14.7-slim-bookworm@sha256:"
        "9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS builder\n"
        "FROM builder AS local\n"
        "FROM node:24.21.0-bookworm-slim AS oops\n"
    )
    external = _external_from_lines(dockerfile)
    assert not any(line.startswith("FROM builder") for line in external), "a prior-stage base must be exempt"
    undigested = [line for line in external if "@sha256:" not in line]
    assert len(undigested) == 1
    assert undigested[0].startswith("FROM node:24.21.0-bookworm-slim")


@pytest.mark.parametrize("service", SERVICES)
def test_every_image_copies_the_readme_pyproject_declares(service: str) -> None:
    """Without README.md every `pip install -e .` warns and ships empty metadata."""
    dockerfile = _dockerfile(service)
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY pyproject.toml")]
    assert copies, f"{service} never copies pyproject.toml"
    assert all(line == "COPY pyproject.toml README.md ./" for line in copies), copies


@pytest.mark.parametrize("service", SERVICES)
def test_every_image_installs_the_observability_extra(service: str) -> None:
    """Every service calls init_tracing(); without the extra it always no-ops."""
    builder_installs = [line for line in _dockerfile(service).splitlines() if "--prefix=/install -e" in line]
    assert builder_installs, f"{service} has no builder-stage package install"
    assert all("observability" in line for line in builder_installs), builder_installs


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("unused", ["google-adk", "google-cloud-secret-manager", "google-cloud-logging", "jira"])
def test_image_requirements_drop_sdks_nothing_imports(service: str, unused: str) -> None:
    requirements = _read(f"containers/{service}/requirements.txt")
    assert not any(line.strip().startswith(unused) for line in requirements.splitlines())


@pytest.mark.parametrize("service", SERVICES)
def test_images_install_no_unused_curl(service: str) -> None:
    assert " curl" not in _dockerfile(service)


def test_ci_image_smoke_imports_the_service_entry_module() -> None:
    """Importing only `henchmen` never touches a service's own dependencies."""
    steps = yaml.dump(_load_yaml(".github/workflows/ci.yml")["jobs"]["docker-build"]["steps"])
    assert "henchmen.operative.bootstrap" in steps
    assert "henchmen.${{ matrix.service }}.server" in steps


def _dockerfile_stages(text: str) -> list[tuple[str, str]]:
    """Return (base, stage name) for each FROM line, in order."""
    stages = []
    for line in text.splitlines():
        parts = line.split()
        if parts[:1] == ["FROM"]:
            name = parts[3] if len(parts) >= 4 and parts[2].upper() == "AS" else ""
            stages.append((parts[1], name))
    return stages


def test_mastermind_dockerfile_default_target_is_still_the_service() -> None:
    stages = _dockerfile_stages(_mastermind_dockerfile())
    assert stages[-1] == ("runtime", "mastermind")


def test_local_stage_runs_serve_from_the_data_volume() -> None:
    text = _mastermind_dockerfile()
    stages = _dockerfile_stages(text)
    assert ("runtime", "local") in stages
    local_block = text.split("AS local", 1)[1].split("\nFROM ", 1)[0]
    assert "HENCHMEN_DATA_DIR=/data" in local_block
    assert "HENCHMEN_PROVIDER=local" in local_block
    assert "HENCHMEN_LOCAL_SQLITE_PATH=/data/henchmen.db" in local_block
    assert 'VOLUME ["/data"]' in local_block
    assert "USER root" in local_block
    assert '"henchmen", "serve", "--host", "0.0.0.0", "--port", "8000"' in local_block


def test_ci_builds_and_smoke_tests_the_local_image() -> None:
    jobs = _load_yaml(".github/workflows/ci.yml")["jobs"]
    steps = " ".join(str(step.get("run", "")) for step in jobs["docker-local"]["steps"])
    assert "--target local" in steps
    assert "/console/api/status" in steps
