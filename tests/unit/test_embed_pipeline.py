"""Unit tests for the code embedding pipeline and its entry points.

Covers :mod:`henchmen.dossier.embed_pipeline` (moved out of Dispatch, which
only normalizes and publishes) and the ``henchmen embed`` CLI command. The
Mastermind ``/pubsub/embed-request`` consumer is tested in
``test_mastermind_server.py``.
"""

import argparse
import subprocess
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from henchmen.config.settings import Settings
from henchmen.dossier import embed_pipeline


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
    monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Pipeline guards
# ---------------------------------------------------------------------------


class TestEmbeddingPipelineGuards:
    @pytest.mark.asyncio
    async def test_rejects_repo_that_is_not_owner_slash_name(self, monkeypatch):
        result = await embed_pipeline.run_embedding_pipeline(
            repo="--upload-pack=evil", mode="full", settings=_settings(monkeypatch)
        )
        assert result["status"] == "failed"
        assert "invalid repo name" in result["error"]

    @pytest.mark.asyncio
    async def test_clone_uses_resolved_default_branch_and_settings_token(self, monkeypatch):
        settings = _settings(monkeypatch, HENCHMEN_GITHUB_TOKEN="ghp-from-settings")

        clone = AsyncMock(side_effect=RuntimeError("stop here"))
        monkeypatch.setattr(embed_pipeline, "clone_repo", clone)
        monkeypatch.setattr(embed_pipeline, "_resolve_default_branch", AsyncMock(return_value="develop"))

        result = await embed_pipeline.run_embedding_pipeline(repo="acme/api", mode="full", settings=settings)

        assert result["status"] == "failed"
        args, kwargs = clone.call_args
        assert args[1] == "develop"
        assert kwargs["token"] == "ghp-from-settings"

    @pytest.mark.asyncio
    async def test_failed_upsert_does_not_advance_last_indexed_commit(self, monkeypatch, tmp_path):
        """A partial upsert must not mark the commit indexed.

        The next incremental run only diffs from the last-indexed commit, so
        advancing it past chunks that failed to upload strands them forever.
        """
        from henchmen.dossier import embedder
        from henchmen.dossier.embedder import UpsertResult

        settings = _settings(monkeypatch)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
        # A real repo, because the pipeline reads HEAD with `git rev-parse`.
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "t"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "seed"],
        ):
            subprocess.run(cmd, cwd=repo_dir, check=True, capture_output=True)

        monkeypatch.setattr(embed_pipeline, "_resolve_default_branch", AsyncMock(return_value="main"))
        monkeypatch.setattr(embed_pipeline, "clone_repo", AsyncMock())
        monkeypatch.setattr(embed_pipeline.tempfile, "mkdtemp", lambda **kw: str(repo_dir))
        # The pipeline imports these inside the function body, so they are
        # attributes of the embedder module, not of the pipeline module.
        monkeypatch.setattr(embedder, "upsert_chunks", AsyncMock(return_value=UpsertResult(uploaded=2, failed=5)))
        set_commit = AsyncMock()
        monkeypatch.setattr(embedder, "set_last_indexed_commit", set_commit)

        result = await embed_pipeline.run_embedding_pipeline(repo="acme/api", mode="full", settings=settings)

        assert result["status"] == "failed"
        assert result["chunks_failed"] == 5
        set_commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# EmbedRequest contract
# ---------------------------------------------------------------------------


class TestEmbedRequest:
    def test_defaults_to_incremental(self):
        request = embed_pipeline.EmbedRequest(repo="acme/api")
        assert request.mode == "incremental"
        assert request.commit_sha == ""

    @pytest.mark.parametrize("body", ['{"repo": "acme/api", "mode": "partial"}', '{"mode": "full"}', '{"repo": ""}'])
    def test_rejects_malformed_messages(self, body):
        with pytest.raises(ValidationError):
            embed_pipeline.EmbedRequest.model_validate_json(body)


# ---------------------------------------------------------------------------
# henchmen embed
# ---------------------------------------------------------------------------


class TestEmbedCli:
    @staticmethod
    def _run(monkeypatch, argv: list[str], result: dict) -> tuple[int, AsyncMock]:
        from henchmen.cli.embed import add_embed_arguments, run_embed_cli

        settings = _settings(monkeypatch)
        parser = argparse.ArgumentParser()
        add_embed_arguments(parser)
        pipeline = AsyncMock(return_value=result)
        with (
            patch("henchmen.config.settings.get_settings", return_value=settings),
            patch("henchmen.dossier.embed_pipeline.run_embedding_pipeline", pipeline),
        ):
            code = run_embed_cli(parser.parse_args(argv))
        return code, pipeline

    @pytest.mark.parametrize(("argv", "mode"), [(["acme/api"], "incremental"), (["acme/api", "--full"], "full")])
    def test_completed_run_exits_zero(self, monkeypatch, argv, mode):
        code, pipeline = self._run(monkeypatch, argv, {"status": "completed", "chunks_upserted": 3})

        assert code == 0
        repo, called_mode, _settings_arg = pipeline.await_args.args
        assert (repo, called_mode) == ("acme/api", mode)

    def test_failed_run_exits_non_zero(self, monkeypatch, capsys):
        code, _pipeline = self._run(monkeypatch, ["acme/api"], {"status": "failed", "error": "2 of 7 chunks failed"})

        assert code == 1
        assert "2 of 7 chunks failed" in capsys.readouterr().err

    def test_crashed_run_exits_non_zero(self, monkeypatch, capsys):
        from henchmen.cli.embed import add_embed_arguments, run_embed_cli

        settings = _settings(monkeypatch)
        parser = argparse.ArgumentParser()
        add_embed_arguments(parser)
        with (
            patch("henchmen.config.settings.get_settings", return_value=settings),
            patch(
                "henchmen.dossier.embed_pipeline.run_embedding_pipeline", AsyncMock(side_effect=RuntimeError("no git"))
            ),
        ):
            assert run_embed_cli(parser.parse_args(["acme/api"])) == 1
        assert "no git" in capsys.readouterr().err

    def test_subcommand_is_wired_into_the_cli(self, monkeypatch):
        from henchmen import cli

        monkeypatch.setattr("sys.argv", ["henchmen", "embed", "acme/api", "--full"])
        with patch("henchmen.cli.embed.run_embed_cli", return_value=0) as run, pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        args = run.call_args.args[0]
        assert (args.repo, args.full) == ("acme/api", True)
