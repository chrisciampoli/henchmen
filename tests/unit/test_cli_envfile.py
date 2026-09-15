"""Unit tests for the ``.env.local`` reader/writer used by ``henchmen init``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from henchmen.cli.envfile import EnvFile, is_secret_key, quote_value

SAMPLE = """# Henchmen local development configuration

# =============================================================================
# PROVIDER SELECTION
# =============================================================================
HENCHMEN_PROVIDER=local
HENCHMEN_ENVIRONMENT=dev

# LLM
HENCHMEN_LLM_PROVIDER=anthropic
HENCHMEN_ANTHROPIC_API_KEY="sk-ant-quoted"
export HENCHMEN_GITHUB_TOKEN=ghp_exported
CUSTOM_UNKNOWN_KEY=keep me   # trailing comment
"""


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env.local"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestLoad:
    def test_missing_file_is_empty(self, tmp_path: Path):
        env = EnvFile.load(tmp_path / ".env.local")
        assert env.as_dict() == {}
        assert env.exists is False

    def test_reads_plain_values(self, env_path: Path):
        env = EnvFile.load(env_path)
        assert env.exists is True
        assert env.get("HENCHMEN_PROVIDER") == "local"
        assert env.get("HENCHMEN_ENVIRONMENT") == "dev"

    def test_strips_double_quotes(self, env_path: Path):
        env = EnvFile.load(env_path)
        assert env.get("HENCHMEN_ANTHROPIC_API_KEY") == "sk-ant-quoted"

    def test_accepts_export_prefix(self, env_path: Path):
        env = EnvFile.load(env_path)
        assert env.get("HENCHMEN_GITHUB_TOKEN") == "ghp_exported"

    def test_unquoted_value_with_trailing_comment(self, env_path: Path):
        env = EnvFile.load(env_path)
        assert env.get("CUSTOM_UNKNOWN_KEY") == "keep me"

    def test_get_default_for_missing(self, env_path: Path):
        env = EnvFile.load(env_path)
        assert env.get("NOPE") == ""
        assert env.get("NOPE", "x") == "x"

    def test_keys_in_file_order(self, env_path: Path):
        env = EnvFile.load(env_path)
        assert env.keys()[:3] == ["HENCHMEN_PROVIDER", "HENCHMEN_ENVIRONMENT", "HENCHMEN_LLM_PROVIDER"]

    def test_commented_assignments_are_not_keys(self, tmp_path: Path):
        path = tmp_path / ".env.local"
        path.write_text("# HENCHMEN_OPENAI_API_KEY=sk-...\nHENCHMEN_PROVIDER=local\n", encoding="utf-8")
        env = EnvFile.load(path)
        assert "HENCHMEN_OPENAI_API_KEY" not in env.as_dict()


# ---------------------------------------------------------------------------
# Mutation + rendering
# ---------------------------------------------------------------------------


class TestSetAndRender:
    def test_set_existing_key_updates_in_place(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("HENCHMEN_ENVIRONMENT", "staging")
        rendered = env.render()
        assert "HENCHMEN_ENVIRONMENT=staging" in rendered
        # In place: still directly after HENCHMEN_PROVIDER, and only once.
        lines = rendered.splitlines()
        idx = lines.index("HENCHMEN_ENVIRONMENT=staging")
        assert lines[idx - 1] == "HENCHMEN_PROVIDER=local"
        assert rendered.count("HENCHMEN_ENVIRONMENT=") == 1

    def test_set_preserves_comments_and_unknown_keys(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("HENCHMEN_PROVIDER", "gcp")
        rendered = env.render()
        assert "# PROVIDER SELECTION" in rendered
        assert "CUSTOM_UNKNOWN_KEY=keep me   # trailing comment" in rendered

    def test_set_new_key_appends_under_section_header(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("HENCHMEN_SLACK_BOT_TOKEN", "xoxb-1", section="Slack")
        env.set("HENCHMEN_SLACK_APP_TOKEN", "xapp-1", section="Slack")
        rendered = env.render()
        assert rendered.count("# ---- Slack ----") == 1
        assert rendered.index("# ---- Slack ----") < rendered.index("HENCHMEN_SLACK_BOT_TOKEN=xoxb-1")
        assert rendered.index("HENCHMEN_SLACK_BOT_TOKEN=xoxb-1") < rendered.index("HENCHMEN_SLACK_APP_TOKEN=xapp-1")

    def test_set_new_key_without_section_appends_at_end(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("NEW_KEY", "v")
        assert env.render().rstrip().endswith("NEW_KEY=v")

    def test_set_quotes_values_with_spaces_or_hash(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("HENCHMEN_GIT_AUTHOR_NAME", "Henchmen Operative")
        env.set("WEIRD", 'a#b "c"')
        rendered = env.render()
        assert 'HENCHMEN_GIT_AUTHOR_NAME="Henchmen Operative"' in rendered
        assert 'WEIRD="a#b \\"c\\""' in rendered
        # Round-trip through the parser gives the original values back.
        reloaded = EnvFile.from_text(rendered)
        assert reloaded.get("HENCHMEN_GIT_AUTHOR_NAME") == "Henchmen Operative"
        assert reloaded.get("WEIRD") == 'a#b "c"'

    def test_set_many(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set_many({"A": "1", "B": "2"}, section="Misc")
        assert env.get("A") == "1" and env.get("B") == "2"
        assert "# ---- Misc ----" in env.render()

    def test_unset_removes_all_occurrences(self, tmp_path: Path):
        path = tmp_path / ".env.local"
        path.write_text("A=1\nB=2\nA=3\n", encoding="utf-8")
        env = EnvFile.load(path)
        env.unset("A")
        assert env.render() == "B=2\n"

    def test_duplicate_keys_last_wins_on_get_and_set_collapses(self, tmp_path: Path):
        path = tmp_path / ".env.local"
        path.write_text("A=1\nB=2\nA=3\n", encoding="utf-8")
        env = EnvFile.load(path)
        assert env.get("A") == "3"
        env.set("A", "9")
        assert env.render() == "A=9\nB=2\n"

    def test_render_ends_with_single_newline(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("X", "1")
        rendered = env.render()
        assert rendered.endswith("\n") and not rendered.endswith("\n\n")


class TestQuoteValue:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("simple", "simple"),
            ("sk-ant-api03-abc_DEF.123", "sk-ant-api03-abc_DEF.123"),
            ("http://localhost:11434", "http://localhost:11434"),
            ("has space", '"has space"'),
            ("has#hash", '"has#hash"'),
            ("", '""'),
            ('q"uote', '"q\\"uote"'),
        ],
    )
    def test_quoting(self, raw: str, expected: str):
        assert quote_value(raw) == expected


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_creates_file_and_backup(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("HENCHMEN_ENVIRONMENT", "staging")
        backup = env.write()
        assert backup == env_path.with_name(".env.local.bak")
        assert backup.read_text(encoding="utf-8") == SAMPLE
        assert "HENCHMEN_ENVIRONMENT=staging" in env_path.read_text(encoding="utf-8")

    def test_write_without_backup(self, env_path: Path):
        env = EnvFile.load(env_path)
        assert env.write(backup=False) is None
        assert not env_path.with_name(".env.local.bak").exists()

    def test_write_new_file_has_no_backup(self, tmp_path: Path):
        path = tmp_path / ".env.local"
        env = EnvFile.load(path)
        env.set("A", "1")
        assert env.write() is None
        assert path.read_text(encoding="utf-8") == "A=1\n"

    def test_write_leaves_no_temp_file_behind(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("A", "1")
        env.write()
        leftovers = [p.name for p in env_path.parent.iterdir() if p.name not in (".env.local", ".env.local.bak")]
        assert leftovers == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_write_sets_owner_only_permissions(self, tmp_path: Path):
        path = tmp_path / ".env.local"
        env = EnvFile.load(path)
        env.set("SECRET", "x")
        env.write()
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_backup_is_owner_only(self, env_path: Path):
        env = EnvFile.load(env_path)
        env.set("HENCHMEN_ENVIRONMENT", "staging")
        backup = env.write()
        assert backup is not None
        assert oct(os.stat(backup).st_mode & 0o777) == "0o600"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_a_world_readable_backup_is_replaced_by_an_owner_only_one(self, env_path: Path):
        backup_path = env_path.with_name(env_path.name + ".bak")
        backup_path.write_bytes(b"stale, world-readable")
        os.chmod(backup_path, 0o644)

        env = EnvFile.load(env_path)
        env.set("HENCHMEN_ENVIRONMENT", "staging")
        env.write()

        assert oct(os.stat(backup_path).st_mode & 0o777) == "0o600"
        assert backup_path.read_text(encoding="utf-8") == SAMPLE

    def test_an_interrupted_main_write_leaves_the_original_intact(
        self, env_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        original = env_path.read_text(encoding="utf-8")
        env = EnvFile.load(env_path)
        env.set("HENCHMEN_ENVIRONMENT", "staging")

        def _boom(_fd: int, _data: bytes) -> int:
            raise OSError("disk full")

        monkeypatch.setattr(os, "write", _boom)
        with pytest.raises(OSError):
            env.write(backup=False)

        assert env_path.read_text(encoding="utf-8") == original
        leftovers = [p.name for p in env_path.parent.iterdir() if p.name != ".env.local"]
        assert leftovers == []


# ---------------------------------------------------------------------------
# is_secret_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "HENCHMEN_GITHUB_TOKEN",
        "HENCHMEN_ANTHROPIC_API_KEY",
        "HENCHMEN_SLACK_SIGNING_SECRET",
        "HENCHMEN_GITHUB_WEBHOOK_SECRET",
        "henchmen_jira_api_token",
    ],
)
def test_credential_keys_are_secret(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS",
        "HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH",
        "HENCHMEN_GITHUB_APP_ID",
        "HENCHMEN_JIRA_PROJECT_KEY",
    ],
)
def test_non_credential_keys_are_not_secret(key: str) -> None:
    assert not is_secret_key(key)
