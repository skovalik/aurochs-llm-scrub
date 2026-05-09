"""Unit tests for core.precommit — install + staged/unstaged refusal."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aurochs_llm_scrub.core.db import MigrationRunner
from aurochs_llm_scrub.core.precommit import (
    _conflict_paths,
    install,
    is_installed,
    run_precommit,
)
from aurochs_llm_scrub.core.rule_pack import register_default_pack

_GIT = shutil.which("git")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    if _GIT is None:
        pytest.skip("git not available on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    # Initial commit so HEAD exists.
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


# ---------------------------------------------------------------------------
# install / is_installed
# ---------------------------------------------------------------------------


class TestInstall:
    def test_fresh_install_writes_hook(self, git_repo: Path) -> None:
        assert not is_installed(git_repo)
        hook = install(git_repo)
        assert hook.exists()
        body = hook.read_text(encoding="utf-8")
        assert "aurochs-llm-scrub" in body
        assert "LLM_SCRUB_PRE_COMMIT=1" in body
        assert is_installed(git_repo)

    def test_idempotent_reinstall(self, git_repo: Path) -> None:
        install(git_repo)
        first_body = (git_repo / ".git/hooks/pre-commit").read_text(
            encoding="utf-8"
        )
        install(git_repo)
        second_body = (git_repo / ".git/hooks/pre-commit").read_text(
            encoding="utf-8"
        )
        # The marker line appears exactly once on idempotent reinstall.
        marker = "# aurochs-llm-scrub: pre-commit"
        assert second_body.count(marker) == 1
        # Body should be effectively identical (one block).
        assert first_body.strip() == second_body.strip()

    def test_appends_to_existing_hook(self, git_repo: Path) -> None:
        # Pre-existing hook from another tool.
        hook = git_repo / ".git/hooks/pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            "#!/bin/sh\n# Existing hook\necho 'other tool'\n",
            encoding="utf-8",
        )
        install(git_repo)
        body = hook.read_text(encoding="utf-8")
        # Existing content preserved; our block appended.
        assert "echo 'other tool'" in body
        assert "aurochs-llm-scrub" in body

    def test_install_outside_repo_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not a git repo"):
            install(tmp_path)


# ---------------------------------------------------------------------------
# Staged / unstaged conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_clean_repo_no_conflict(self, git_repo: Path) -> None:
        assert _conflict_paths(git_repo) == []

    def test_only_staged_no_conflict(self, git_repo: Path) -> None:
        # Add a new file and stage it; no conflict because there's no
        # unstaged version of the same file.
        (git_repo / "new.md").write_text("hi\n", encoding="utf-8")
        _git("add", "new.md", cwd=git_repo)
        assert _conflict_paths(git_repo) == []

    def test_staged_and_unstaged_same_file(self, git_repo: Path) -> None:
        # BLOCKER #5 trigger: file has staged AND unstaged changes.
        target = git_repo / "draft.md"
        target.write_text("v1\n", encoding="utf-8")
        _git("add", "draft.md", cwd=git_repo)
        _git("commit", "-q", "-m", "v1", cwd=git_repo)

        target.write_text("v2\n", encoding="utf-8")
        _git("add", "draft.md", cwd=git_repo)
        # Now modify again WITHOUT staging.
        target.write_text("v3\n", encoding="utf-8")

        conflicts = _conflict_paths(git_repo)
        assert "draft.md" in conflicts


# ---------------------------------------------------------------------------
# run_precommit — full path
# ---------------------------------------------------------------------------


class TestRunPrecommit:
    def test_clean_commit_returns_zero(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        db = tmp_path / "runs.db"
        MigrationRunner(db).run()
        register_default_pack(db)

        # Stage a clean file.
        (git_repo / "clean.md").write_text(
            "Stefan writes direct prose.\n", encoding="utf-8"
        )
        _git("add", "clean.md", cwd=git_repo)

        rc = run_precommit(git_repo, db)
        assert rc == 0

    def test_em_dash_auto_fixed_then_zero(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        db = tmp_path / "runs.db"
        MigrationRunner(db).run()
        register_default_pack(db)

        target = git_repo / "draft.md"
        target.write_text("paused — then\n", encoding="utf-8")
        _git("add", "draft.md", cwd=git_repo)

        rc = run_precommit(git_repo, db)
        # Soft mode: even with a finding, auto-fix succeeds and return 0.
        assert rc == 0
        # The file on disk has been auto-fixed.
        assert "—" not in target.read_text(encoding="utf-8")
        # And it has been re-staged (git diff --cached --name-only
        # should still show draft.md).
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "draft.md" in out

    def test_staged_unstaged_conflict_returns_two(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        db = tmp_path / "runs.db"
        MigrationRunner(db).run()
        register_default_pack(db)

        target = git_repo / "draft.md"
        target.write_text("v1\n", encoding="utf-8")
        _git("add", "draft.md", cwd=git_repo)
        _git("commit", "-q", "-m", "v1", cwd=git_repo)

        target.write_text("v2\n", encoding="utf-8")
        _git("add", "draft.md", cwd=git_repo)
        target.write_text("v3\n", encoding="utf-8")

        rc = run_precommit(git_repo, db)
        assert rc == 2
