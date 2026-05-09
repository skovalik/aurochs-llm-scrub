"""Default file-system paths for aurochs-llm-scrub.

Centralizes the ``~/.aurochs-llm-scrub/`` (or
``%LOCALAPPDATA%\\aurochs-llm-scrub`` on Windows) layout so every
subcommand resolves the same canonical locations. ``platformdirs`` is
the preferred backend (plan v4 cross-cutting fix #19); without it we
fall back to env vars + ``~`` defaults.

Layout::

    <data_dir>/
        runs.db                     sqlite store (opt-in history surface)
        runs.db.write.lock          write-lock advisory file
        runs.db.migrate.lock        migrate-lock advisory file
        custom-rules.toml           user custom rules
        exemptions.toml             workspace exemption baseline
        config.toml                 engine config + [redact] namespace
        disabled-rules.log          ReDoS auto-disable audit log
        manifest.json               cross-plugin schema_version pin
        tmp/                        atomic-rename staging dir
        locks/                      OS lockfile dir
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "aurochs-llm-scrub"
_ENV_DATA_DIR = "AUROCHS_LLM_SCRUB_HOME"
_ENV_DB = "AUROCHS_LLM_SCRUB_DB"


def data_dir() -> Path:
    """Return the per-user data directory.

    Resolution order:
      1. ``$AUROCHS_LLM_SCRUB_HOME`` (test override / explicit user pin).
      2. ``platformdirs.user_data_dir("aurochs-llm-scrub")`` if installed.
      3. ``%LOCALAPPDATA%\\aurochs-llm-scrub`` on Windows /
         ``~/.local/share/aurochs-llm-scrub`` elsewhere as a last-resort
         fallback.
    """
    env = os.environ.get(_ENV_DATA_DIR)
    if env:
        return Path(env).expanduser()

    try:
        import platformdirs

        return Path(platformdirs.user_data_dir(_APP_NAME))
    except ImportError:
        pass

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / _APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / _APP_NAME


def default_db_path() -> Path:
    """Path to ``runs.db``. ``$AUROCHS_LLM_SCRUB_DB`` overrides everything."""
    env = os.environ.get(_ENV_DB)
    if env:
        return Path(env).expanduser()
    return data_dir() / "runs.db"


def default_custom_rules_path(db_path: Path | None = None) -> Path:
    base = (db_path or default_db_path()).parent
    return base / "custom-rules.toml"


def default_exemptions_path(db_path: Path | None = None) -> Path:
    base = (db_path or default_db_path()).parent
    return base / "exemptions.toml"


def default_config_path(db_path: Path | None = None) -> Path:
    base = (db_path or default_db_path()).parent
    return base / "config.toml"


def default_disabled_log_path(db_path: Path | None = None) -> Path:
    base = (db_path or default_db_path()).parent
    return base / "disabled-rules.log"


def default_manifest_path(db_path: Path | None = None) -> Path:
    base = (db_path or default_db_path()).parent
    return base / "manifest.json"


def add_db_path_arg(parser: object) -> None:
    """Attach ``--db-path`` to a subcommand parser.

    Argparse only resolves global flags BEFORE subcommands; a user typing
    ``llm-scrub scan --db-path X file.md`` expects it to work. We register
    the flag at both levels so it parses regardless of position. Default
    is ``argparse.SUPPRESS`` so the handler can fall back to the
    top-level ``--db-path`` when not repeated after the subcommand.
    """
    import argparse as _ap

    if not isinstance(parser, _ap.ArgumentParser):
        raise TypeError(f"expected ArgumentParser, got {type(parser).__name__}")
    parser.add_argument(
        "--db-path",
        dest="db_path",
        default=_ap.SUPPRESS,
        help="Path to runs.db (overrides default per-user data dir).",
    )


def ensure_layout(db_path: Path | None = None) -> Path:
    """Create the standard sub-directories next to ``runs.db``.

    Returns the resolved data directory. Idempotent.
    """
    base = (db_path or default_db_path()).parent
    for sub in ("tmp", "locks"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def resolve_db_path(args: object) -> Path:
    """Resolve ``--db-path`` from a parsed namespace, falling back to default."""
    db_path_arg = getattr(args, "db_path", None)
    if db_path_arg:
        return Path(db_path_arg).expanduser().resolve()
    return default_db_path()
