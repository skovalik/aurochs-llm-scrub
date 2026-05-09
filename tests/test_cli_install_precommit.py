"""Tests for ``llm-scrub install-precommit``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "aurochs_llm_scrub.cli.main", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=60,
    )


def _make_fake_repo(tmp_path: Path) -> Path:
    """Create a minimal fake .git/hooks structure for hook installation."""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


def test_install_precommit_help() -> None:
    proc = _run(["install-precommit", "--help"])
    assert proc.returncode == 0
    assert "--mode" in proc.stdout


def test_install_precommit_outside_repo(tmp_path: Path) -> None:
    proc = _run(["install-precommit", "--repo-root", str(tmp_path)])
    assert proc.returncode == 2
    assert "git" in proc.stderr.lower() or "repository" in proc.stderr.lower()


def test_install_precommit_soft_writes_hook(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    proc = _run(["install-precommit", "--repo-root", str(repo), "--mode", "soft"])
    assert proc.returncode == 0, proc.stderr
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    body = hook.read_text(encoding="utf-8")
    assert "LLM_SCRUB_PRE_COMMIT" in body
    assert "llm-scrub scan" in body
    # Soft mode does NOT add --hard-fail
    assert "--hard-fail" not in body


def test_install_precommit_hard_includes_hard_fail(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    proc = _run(["install-precommit", "--repo-root", str(repo), "--mode", "hard"])
    assert proc.returncode == 0, proc.stderr
    hook = repo / ".git" / "hooks" / "pre-commit"
    body = hook.read_text(encoding="utf-8")
    assert "--hard-fail" in body


def test_install_precommit_existing_hook_blocks_without_force(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("# existing\n", encoding="utf-8")
    proc = _run(["install-precommit", "--repo-root", str(repo)])
    assert proc.returncode == 1
    assert "already exists" in proc.stderr.lower()
    assert hook.read_text(encoding="utf-8") == "# existing\n"


def test_install_precommit_force_overwrites(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("# existing\n", encoding="utf-8")
    proc = _run(["install-precommit", "--repo-root", str(repo), "--force"])
    assert proc.returncode == 0, proc.stderr
    body = hook.read_text(encoding="utf-8")
    assert "LLM_SCRUB_PRE_COMMIT" in body
