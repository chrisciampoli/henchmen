"""Unit tests for the dossier convention detector module."""

import json

from henchmen.dossier.convention_detector import (
    RepoConventions,
    conventions_to_prompt,
    detect_conventions,
)

# ---------------------------------------------------------------------------
# detect_conventions — Python project fixtures
# ---------------------------------------------------------------------------


class TestDetectPythonProject:
    def test_detects_pytest_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n',
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.test_framework == "pytest"

    def test_detects_ruff_lint(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length = 120\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.lint_config == "ruff"

    def test_detects_mypy_strict(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\nstrict = true\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.type_system == "mypy-strict"

    def test_detects_mypy_non_strict(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\ncheck_untyped_defs = true\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.type_system == "mypy"

    def test_detects_poetry_package_manager(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry]\nname = 'myproject'\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.package_manager == "poetry"

    def test_detects_flake8(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.flake8]\nmax-line-length = 100\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.lint_config == "flake8"


# ---------------------------------------------------------------------------
# detect_conventions — JavaScript/TypeScript project fixtures
# ---------------------------------------------------------------------------


class TestDetectJSProject:
    def test_detects_jest_from_package_json(self, tmp_path):
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert result.test_framework == "jest"

    def test_detects_vitest(self, tmp_path):
        pkg = {"devDependencies": {"vitest": "^1.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert result.test_framework == "vitest"

    def test_detects_eslint(self, tmp_path):
        pkg = {"devDependencies": {"eslint": "^8.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert result.lint_config == "eslint"

    def test_detects_eslint_plus_prettier(self, tmp_path):
        pkg = {"devDependencies": {"eslint": "^8.0.0", "prettier": "^3.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert "eslint" in (result.lint_config or "")
        assert "prettier" in (result.lint_config or "")

    def test_detects_typescript_strict(self, tmp_path):
        pkg = {"devDependencies": {"typescript": "^5.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        tsconfig = {"compilerOptions": {"strict": True}}
        (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig), encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert result.type_system == "typescript-strict"

    def test_detects_pnpm_from_lockfile(self, tmp_path):
        pkg = {"name": "test"}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 6.0\n", encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert result.package_manager == "pnpm"

    def test_detects_npm_from_lockfile(self, tmp_path):
        pkg = {"name": "test"}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert result.package_manager == "npm"


# ---------------------------------------------------------------------------
# detect_conventions — source file sampling
# ---------------------------------------------------------------------------


class TestDetectFromSourceFiles:
    def test_detects_snake_case_python(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "handler.py").write_text(
            "def handle_request():\n    pass\n\ndef process_data():\n    pass\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.naming_convention == "snake_case"

    def test_detects_four_space_indent(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text(
            "def foo():\n    x = 1\n    y = 2\n    return x + y\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.indentation == "4-space"

    def test_detects_absolute_imports(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "module.py").write_text(
            "from mypackage.utils import helper\nfrom mypackage.config import settings\n",
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.import_style == "absolute"

    def test_empty_workspace_returns_empty_conventions(self, tmp_path):
        result = detect_conventions(str(tmp_path))
        assert result.test_framework is None
        assert result.lint_config is None
        assert result.naming_convention is None


# ---------------------------------------------------------------------------
# detect_conventions — package manager detection
# ---------------------------------------------------------------------------


class TestDetectPackageManager:
    def test_detects_pip_from_setuptools(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n',
            encoding="utf-8",
        )
        result = detect_conventions(str(tmp_path))
        assert result.package_manager == "pip"

    def test_detects_yarn_from_lockfile(self, tmp_path):
        pkg = {"name": "test"}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        (tmp_path / "yarn.lock").write_text("# yarn lockfile\n", encoding="utf-8")
        result = detect_conventions(str(tmp_path))
        assert result.package_manager == "yarn"


# ---------------------------------------------------------------------------
# conventions_to_prompt
# ---------------------------------------------------------------------------


class TestConventionsToPrompt:
    def test_formats_detected_conventions(self):
        conventions = RepoConventions(
            test_framework="pytest",
            lint_config="ruff",
            naming_convention="snake_case",
            indentation="4-space",
        )
        prompt = conventions_to_prompt(conventions)
        assert "pytest" in prompt
        assert "ruff" in prompt
        assert "snake_case" in prompt
        assert "4-space" in prompt
        assert "## Detected Project Conventions" in prompt

    def test_returns_empty_for_no_conventions(self):
        conventions = RepoConventions()
        prompt = conventions_to_prompt(conventions)
        assert prompt == ""

    def test_includes_only_detected_fields(self):
        conventions = RepoConventions(test_framework="jest")
        prompt = conventions_to_prompt(conventions)
        assert "jest" in prompt
        assert "ruff" not in prompt
        assert "mypy" not in prompt

    def test_model_validates(self):
        c = RepoConventions(
            test_framework="pytest",
            import_style="absolute",
            error_handling="try/except",
            type_system="mypy-strict",
            naming_convention="snake_case",
            indentation="4-space",
            lint_config="ruff",
            package_manager="pip",
        )
        assert c.test_framework == "pytest"
        assert c.package_manager == "pip"
