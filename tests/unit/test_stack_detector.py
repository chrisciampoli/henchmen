"""Unit tests for the stack detector."""

from __future__ import annotations

from henchmen.utils.stack_detector import Stack, detect_stack

# ---------------------------------------------------------------------------
# detect_stack
# ---------------------------------------------------------------------------


class TestDetectStack:
    def test_python_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
        assert detect_stack(tmp_path).name == "python"

    def test_python_setup_py(self, tmp_path):
        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
        assert detect_stack(tmp_path).name == "python"

    def test_python_requirements_txt(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("pytest\n")
        assert detect_stack(tmp_path).name == "python"

    def test_node_pnpm_monorepo(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pnpm-lock.yaml").write_text("")
        (tmp_path / "turbo.json").write_text("{}")
        stack = detect_stack(tmp_path)
        assert stack.name == "node-pnpm"
        assert stack.is_monorepo is True

    def test_node_pnpm_single_package(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pnpm-lock.yaml").write_text("")
        stack = detect_stack(tmp_path)
        assert stack.name == "node-pnpm"
        assert stack.is_monorepo is False

    def test_node_npm(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        assert detect_stack(tmp_path).name == "node-npm"

    def test_node_generic_fallback(self, tmp_path):
        # package.json but no lockfile — still a Node project
        (tmp_path / "package.json").write_text("{}")
        assert detect_stack(tmp_path).name == "node-npm"

    def test_go_module(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        assert detect_stack(tmp_path).name == "go"

    def test_rust_cargo(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"\n')
        assert detect_stack(tmp_path).name == "rust"

    def test_java_maven(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>\n")
        assert detect_stack(tmp_path).name == "java-maven"

    def test_java_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("")
        assert detect_stack(tmp_path).name == "java-gradle"

    def test_java_gradle_kts(self, tmp_path):
        (tmp_path / "build.gradle.kts").write_text("")
        assert detect_stack(tmp_path).name == "java-gradle"

    def test_unknown_stack(self, tmp_path):
        (tmp_path / "README.md").write_text("# test\n")
        assert detect_stack(tmp_path).name == "unknown"

    def test_priority_python_over_node(self, tmp_path):
        """When both pyproject.toml and package.json exist, Python wins.

        This reflects the typical Python project layout where a pyproject.toml
        is the authoritative build file and package.json might exist only for
        dev tooling like markdown linters.
        """
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (tmp_path / "package.json").write_text("{}")
        assert detect_stack(tmp_path).name == "python"

    def test_stack_has_test_command(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
        stack = detect_stack(tmp_path)
        assert stack.test_command == ["python", "-m", "pytest", "--no-header", "-q"]

    def test_stack_has_lint_command(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
        stack = detect_stack(tmp_path)
        assert stack.lint_command == ["python", "-m", "ruff", "check", "."]


class TestStackDataclass:
    def test_stack_is_hashable(self):
        a = Stack(name="python", test_command=["pytest"], lint_command=["ruff", "check"], install_command=None)
        b = Stack(name="python", test_command=["pytest"], lint_command=["ruff", "check"], install_command=None)
        # Two Stacks built from the same inputs compare equal.
        assert a == b

    def test_stack_is_monorepo_defaults_false(self):
        s = Stack(name="rust", test_command=["cargo", "test"], lint_command=["cargo", "clippy"], install_command=None)
        assert s.is_monorepo is False

    def test_unknown_stack_empty_commands(self, tmp_path):
        (tmp_path / "README.md").write_text("")
        stack = __import__("henchmen.utils.stack_detector", fromlist=["detect_stack"]).detect_stack(tmp_path)
        assert stack.test_command == []
        assert stack.lint_command == []
