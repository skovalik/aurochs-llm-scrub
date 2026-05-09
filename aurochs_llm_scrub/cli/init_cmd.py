"""``llm-scrub init`` — bootstrap runs.db + sidecar config files.

Idempotent. Re-running on an initialized layout is a no-op (no warnings,
no overwrite). The migration runner stamps schema_version; sidecar
config files get scaffolded only when missing.

Output is intentionally terse — voice/recall pattern: one line per
object created, no decorative banners.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from aurochs_llm_scrub.cli.paths import (
    add_db_path_arg,
    default_config_path,
    default_custom_rules_path,
    default_exemptions_path,
    default_manifest_path,
    ensure_layout,
    resolve_db_path,
)


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "init",
        help="Create runs.db + sidecar config files (idempotent).",
    )
    add_db_path_arg(p)
    p.set_defaults(_handler=run)


def run(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)

    # 1. Layout
    base = ensure_layout(db_path)

    # 2. runs.db — apply migrations under MigrateLock.
    from aurochs_llm_scrub.core import MigrationRunner, register_default_pack

    runner = MigrationRunner(db_path)
    runner.run()  # raises MigrationError on partial state; bubbles up

    # 2b. Register the bundled default rule pack. Idempotent — a re-run
    # of `init` on an already-initialized DB returns the existing pack
    # id without inserting a duplicate, per `register_pack`'s
    # INSERT OR IGNORE policy. Without this call, `scan` / `scrub`
    # against a fresh DB would silently report zero findings (no rules
    # registered) — the user-confusing failure mode this line prevents.
    #
    # Errors during default-pack registration are surfaced as warnings,
    # not fatals: the engine still runs against an empty rule set, and
    # the operator can re-register manually via `llm-scrub rules
    # migrate` (when v0.2 ships) or by re-running init after fixing the
    # underlying issue. We don't want a corrupt default-pack to lock a
    # user out of the rest of the CLI.
    try:
        register_default_pack(db_path)
    except Exception as exc:
        # Warn-and-continue: a failed default-pack registration shouldn't
        # lock the operator out of the rest of the CLI. The ``rules
        # migrate`` path (v0.2) provides a recovery surface; for v0.1
        # the operator can re-run ``init`` after fixing the underlying
        # issue (corrupt default_rules.toml, FS permissions, etc.).
        print(
            f"llm-scrub: warning: could not register default rule pack: {exc}",
            file=sys.stderr,
        )

    # 3. Sidecar config files — scaffold only when missing.
    custom_rules_path = default_custom_rules_path(db_path)
    if not custom_rules_path.exists():
        custom_rules_path.write_text(_starter_custom_rules_toml(), encoding="utf-8")

    exemptions_path = default_exemptions_path(db_path)
    if not exemptions_path.exists():
        exemptions_path.write_text(_starter_exemptions_toml(), encoding="utf-8")

    config_path = default_config_path(db_path)
    if not config_path.exists():
        config_path.write_text(_starter_config_toml(), encoding="utf-8")

    # 4. manifest.json — cross-plugin schema_version pin (plan v4 #14).
    _write_manifest(db_path)

    print(f"runs.db          {db_path}")
    print(f"custom-rules     {custom_rules_path}")
    print(f"exemptions       {exemptions_path}")
    print(f"config           {config_path}")
    print(f"data_dir         {base}")
    return 0


def _starter_custom_rules_toml() -> str:
    return (
        "# aurochs-llm-scrub — custom rules\n"
        "#\n"
        "# Add user-specific detection rules here. Schema (v0.1):\n"
        "#\n"
        "#   [[rule]]\n"
        '#   id = "stefan-pixel-perfect"\n'
        '#   severity = "P0"            # P0 | P1 | P2\n'
        '#   pattern = "pixel-perfect"\n'
        '#   pattern_type = "literal"   # "regex" or "literal"\n'
        "#   case_sensitive = false     # default false (literal)\n"
        "#   whole_word = true          # default true (literal)\n"
        '#   reason = "Banned per BOOK-PROTOCOL"\n'
        '#   language = "en"            # optional; default null = always-on\n'
        "#\n"
        "# See docs/custom-rules.md for full schema reference.\n"
        "\n"
        "schema_version = \"0.1.0\"\n"
    )


def _starter_exemptions_toml() -> str:
    return (
        "# aurochs-llm-scrub — workspace exemptions\n"
        "#\n"
        "# Per-rule workspace-wide exemptions. Cascade order:\n"
        "#   workspace (this file) → directory (.scrub-exempt) → sidecar (<file>.scrub-exempt)\n"
        "#\n"
        "# Example:\n"
        "#   [[exempt]]\n"
        '#   rule = "p0-em-dash"\n'
        '#   reason = "Editorial style: em dashes are intentional"\n'
        "\n"
        "schema_version = \"0.1.0\"\n"
    )


def _starter_config_toml() -> str:
    return (
        "# aurochs-llm-scrub — engine config\n"
        "\n"
        "[engine]\n"
        "timeout_seconds = 1.0           # per-rule-per-chunk wall-clock\n"
        "chunk_size_bytes = 524288       # 512KB; only triggers for inputs >2MB\n"
        "chunk_overlap_bytes = 4096      # 4KB overlap between chunks\n"
        "\n"
        "[language]\n"
        "gate_enabled = true\n"
        "gate_confidence_threshold = 0.8\n"
        'extras_installed = ["en"]\n'
        "\n"
        "[redact]\n"
        "enabled = true\n"
        'patterns = ["aws", "github", "openai", "anthropic", "high-entropy"]\n'
        "\n"
        "[severity_overrides]\n"
        "# pronoun-we = \"P0\"            # voice-equivalent strict mode\n"
        "\n"
        "[output]\n"
        'default_format = "plain"        # plain | json | markdown\n'
        "show_auto_disabled_footer = true\n"
    )


def _write_manifest(db_path: Path) -> None:
    """Write manifest.json (cross-plugin compat marker)."""
    from aurochs_llm_scrub.core import db_connect
    from aurochs_llm_scrub.core.schema import SCHEMA_VERSION, current_schema_version

    manifest_path = default_manifest_path(db_path)
    conn = db_connect(db_path)
    try:
        ver = current_schema_version(conn)
    finally:
        conn.close()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "schema_version_int": ver,
        "runs_db_path": str(db_path),
        "written_at": int(time.time()),
    }
    try:
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as e:
        print(f"llm-scrub: warning: could not write manifest.json: {e}", file=sys.stderr)
