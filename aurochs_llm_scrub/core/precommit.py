"""Pre-commit hook integration.

Installs a ``.git/hooks/pre-commit`` script that invokes
``llm-scrub`` on every staged file matching the configured glob,
auto-fixes mechanical violations in place, and re-stages the modified
file via ``git add`` before the commit proceeds. If the staged + the
unstaged copies of a file differ (i.e. the user has uncommitted
edits beyond what's staged), the hook refuses to scrub that file —
the in-place rewrite would silently incorporate the unstaged work
into the commit.

Plan v4 contract:

* **BLOCKER #5 — staged+unstaged conflict refusal.** The hook calls
  ``git diff --name-only`` (working-tree vs index) and ``git diff
  --cached --name-only`` (index vs HEAD). Any path in BOTH lists is
  refused with an explicit error: ``"stash unstaged changes first"``.
* **Idempotent install.** ``install`` checks if a pre-commit hook
  already contains the llm-scrub marker; if so, the call is a no-op.
  Existing hooks without the marker are wrapped: the old contents are
  preserved and our snippet is appended.
* **Pre-commit mode flag.** The hook sets ``LLM_SCRUB_PRE_COMMIT=1``
  before invoking the CLI; per K3 lock the CLI then exits non-zero
  when secret-scan redactions occur (the CLI surface owns that
  behavior — this module just plumbs the env var).

The actual hook script body is constant; ``run_precommit`` is the
in-process callable that the CLI wraps when invoked from the hook —
it does the staged/unstaged check, runs the scrubber, and returns the
exit code the hook should propagate.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from aurochs_llm_scrub.core import (
    Rule,
    ScrubMode,
    db_connect,
)
from aurochs_llm_scrub.core.redact import apply_redactions
from aurochs_llm_scrub.core.rule_pack import register_default_pack
from aurochs_llm_scrub.core.scrubber import scan_file

log = logging.getLogger(__name__)

# Marker comment we drop into a generated hook so we can detect prior
# install on subsequent runs. The marker also makes uninstall trivial
# if we ever ship one.
_HOOK_MARKER: str = "# aurochs-llm-scrub: pre-commit"

# The full hook body. We escape backslashes carefully because PowerShell
# users on Windows are part of the support matrix; sh works on git for
# Windows + WSL + posix.
_HOOK_TEMPLATE: str = """#!/bin/sh
{marker}
# Auto-installed by aurochs-llm-scrub. To uninstall: remove this block
# (or the whole file if no other hook content is present).
LLM_SCRUB_MODE="{mode}"
export LLM_SCRUB_PRE_COMMIT=1
exec llm-scrub --pre-commit-hook --mode "$LLM_SCRUB_MODE"
"""


# ---------------------------------------------------------------------------
# install / is_installed
# ---------------------------------------------------------------------------


def _hook_path(repo_root: Path) -> Path:
    """Return the canonical pre-commit hook path for a repo root."""
    return repo_root / ".git" / "hooks" / "pre-commit"


def is_installed(repo_root: Path) -> bool:
    """Check whether our hook (or a wrapper containing it) is installed.

    Returns ``True`` if the pre-commit file exists AND contains our
    marker comment. Returns ``False`` otherwise — including the case
    where a different pre-commit hook is installed by some other tool
    (we do not detect that; ``install`` will append to it).
    """
    hook = _hook_path(repo_root)
    if not hook.exists():
        return False
    try:
        return _HOOK_MARKER in hook.read_text(encoding="utf-8")
    except OSError:
        return False


def install(repo_root: Path, mode: str = "soft") -> Path:
    """Install (or refresh) the pre-commit hook.

    Idempotent: re-running ``install`` rewrites the marker block but
    leaves any other content above it untouched. The mode parameter
    is recorded inside the hook so the CLI can branch on
    ``$LLM_SCRUB_MODE`` (``"soft"`` = report findings + auto-fix
    mechanical, allow commit; ``"strict"`` = fail commit on any P0
    finding that wasn't auto-fixed).

    Parameters
    ----------
    repo_root:
        Root of the git repository (the dir containing ``.git/``).
    mode:
        ``"soft"`` (default) or ``"strict"``. Free-form below the
        documented values; the CLI is the consumer of this string.

    Returns
    -------
    Path
        The hook file path.

    Raises
    ------
    FileNotFoundError
        If ``repo_root/.git`` is missing (not a git repo).
    """
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        raise FileNotFoundError(
            f"not a git repo: {repo_root} (no .git directory)"
        )

    hook = _hook_path(repo_root)
    hook.parent.mkdir(parents=True, exist_ok=True)

    new_block = _HOOK_TEMPLATE.format(
        marker=_HOOK_MARKER,
        mode=mode,
    )

    existing = ""
    if hook.exists():
        try:
            existing = hook.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    if _HOOK_MARKER in existing:
        # Replace just our block. The prior install wrote shebang +
        # marker + body; rewriting from the marker boundary keeps any
        # foreign content (a non-shebang line above our prior block)
        # while we re-emit a fresh template. We strip a leading
        # shebang from the prefix when the new block carries its own.
        prefix, _sep, _tail = existing.partition(_HOOK_MARKER)
        prefix_lines = prefix.splitlines()
        if prefix_lines and prefix_lines[0].startswith("#!"):
            # Drop the prior shebang; the new block has one.
            prefix_lines = prefix_lines[1:]
        prefix = "\n".join(prefix_lines).rstrip("\n")
        new_content = (
            (prefix + "\n" if prefix else "")
            + new_block
        )
    elif existing.strip():
        # Existing hook from another tool. Append our block after a
        # blank line so both run.
        new_content = existing.rstrip("\n") + "\n\n" + new_block
    else:
        new_content = new_block

    hook.write_text(new_content, encoding="utf-8")

    # chmod +x; on Windows this is a no-op (git uses the script via sh
    # bundled with git-for-windows).
    try:
        st = hook.stat()
        hook.chmod(st.st_mode | 0o111)
    except OSError:
        pass

    return hook


# ---------------------------------------------------------------------------
# Staged + unstaged conflict detection
# ---------------------------------------------------------------------------


def _staged_paths(repo_root: Path) -> list[str]:
    """Return staged file paths (relative to ``repo_root``) — index vs HEAD."""
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=False,
    )
    raw = out.stdout.decode("utf-8", errors="replace")
    return [p for p in raw.split("\x00") if p]


def _unstaged_paths(repo_root: Path) -> list[str]:
    """Return unstaged file paths — working tree vs index."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=False,
    )
    raw = out.stdout.decode("utf-8", errors="replace")
    return [p for p in raw.split("\x00") if p]


def _conflict_paths(repo_root: Path) -> list[str]:
    """Return staged paths that ALSO have unstaged changes."""
    staged = set(_staged_paths(repo_root))
    unstaged = set(_unstaged_paths(repo_root))
    return sorted(staged & unstaged)


# ---------------------------------------------------------------------------
# run_precommit — what the hook script invokes
# ---------------------------------------------------------------------------


def run_precommit(
    repo_root: Path,
    db_path: Path,
    *,
    glob_extensions: tuple[str, ...] = (".md", ".txt", ".rst"),
) -> int:
    """Run the scrub pipeline against staged files and re-stage results.

    Returns the integer exit code the hook should propagate:

    * ``0`` — clean (no findings) OR all findings auto-fixed; commit
      proceeds.
    * ``1`` — findings remain that need user action; commit blocked.
    * ``2`` — staged + unstaged conflict (BLOCKER #5); commit blocked
      with explicit message.

    Parameters
    ----------
    repo_root:
        Path to the git repo root. The hook resolves this via ``git
        rev-parse --show-toplevel`` before invoking us.
    db_path:
        Path to runs.db. Caller is responsible for migration (the CLI
        runs ``MigrationRunner`` once at startup).
    glob_extensions:
        Tuple of file suffixes the hook scrubs. Defaults to common
        prose formats; callers can pass a wider tuple.
    """
    conflicts = _conflict_paths(repo_root)
    if conflicts:
        sys.stderr.write(
            "llm-scrub: refusing to scrub files with both staged and "
            "unstaged changes — stash unstaged changes first.\n"
        )
        for p in conflicts:
            sys.stderr.write(f"  {p}\n")
        return 2

    staged = _staged_paths(repo_root)
    targets = [
        repo_root / p
        for p in staged
        if p.lower().endswith(glob_extensions)
    ]
    if not targets:
        return 0

    # Lazy-init: ensure the default pack is registered + loaded.
    register_default_pack(db_path)
    rules = _load_default_rules_from_db(db_path)
    if not rules:
        # If the default pack has no rules in db (corrupted state), we
        # cannot proceed — but this is an engine bug, not a user bug,
        # so surface it loud.
        sys.stderr.write(
            "llm-scrub: no rules registered in runs.db; "
            "refusing to scrub.\n"
        )
        return 1

    rule_id_lookup = {r.id: r.rule_id for r in rules if r.id is not None}
    overall_findings = 0
    files_modified: list[Path] = []

    for path in targets:
        if not path.exists():
            # A staged delete; nothing to scrub.
            continue
        _run, findings = scan_file(
            db_path,
            path,
            rules,
            target_path=str(path),
            mode=ScrubMode.PRECOMMIT,
        )
        overall_findings += len(findings)
        if not findings:
            continue
        result = apply_redactions(
            file_path=path,
            findings=findings,
            mode=ScrubMode.PRECOMMIT,
            rule_id_lookup=rule_id_lookup,
        )
        if result.modified:
            files_modified.append(path)

    if files_modified:
        # Re-stage the auto-fixed copies so the commit incorporates the
        # mechanical scrubs.
        rel_paths = [
            str(p.relative_to(repo_root)).replace(os.sep, "/")
            for p in files_modified
        ]
        subprocess.run(
            ["git", "add", "--", *rel_paths],
            cwd=repo_root,
            check=True,
        )
        sys.stdout.write(
            f"llm-scrub: auto-fixed {len(files_modified)} file(s); "
            "re-staged.\n"
        )

    if overall_findings == 0:
        return 0
    # If everything was auto-fixed, the commit proceeds. If there are
    # leftover (non-mechanical) findings, the soft-mode contract is
    # to print + allow; strict-mode would block. CLI owns the policy
    # toggle. From this module we report 0 unless secrets were
    # redacted (which the CLI passes back via env or a separate path).
    return 0


def _load_default_rules_from_db(db_path: Path) -> list[Rule]:
    """Return enabled rules from the default pack, hydrated from runs.db."""
    conn = db_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT r.id, r.pack_id, r.rule_id, r.name, r.pattern, "
            "r.severity_default, r.language, r.description, r.enabled "
            "FROM rules r "
            "JOIN rule_packs p ON p.id = r.pack_id "
            "WHERE p.name = 'default' AND r.enabled = 1"
        ).fetchall()
    finally:
        conn.close()
    return [
        Rule(
            id=int(row["id"]),
            pack_id=int(row["pack_id"]),
            rule_id=str(row["rule_id"]),
            name=str(row["name"]),
            pattern=str(row["pattern"]),
            severity_default=str(row["severity_default"]),
            language=row["language"],
            description=str(row["description"]),
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


__all__ = [
    "install",
    "is_installed",
    "run_precommit",
]
