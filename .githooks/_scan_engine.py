#!/usr/bin/env python3
"""Pre-commit pattern scanner for aurochs-recall.

Walks staged file CONTENTS (not just diff lines) looking for matches against
one or more regex pattern files. BLOCKS the commit on any hit by exiting with
a non-zero status. The matched file path and matched pattern are printed.

Usage:
    _scan_engine.py --label PII --rules .githooks/pii-rules-generic.txt [--rules .pii-rules.local]
    _scan_engine.py --label SECRETS --rules .githooks/secret-rules.txt

Behavior:
    * Reads `git diff --cached --name-only --diff-filter=ACMR` for staged paths.
    * Skips files with extensions outside the allow-list (configurable below).
    * Skips files >10 MB (size cap).
    * Skips files that look binary (NUL byte in first 8 KB).
    * Reads with utf-8 then falls back to latin-1; never crashes on encoding.
    * Optional `--rules` files that don't exist are silently skipped (so a
      missing .pii-rules.local doesn't break anyone's commit).
    * `--no-verify` is the documented bypass.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Configuration — extensions whose contents we walk.
# -----------------------------------------------------------------------------
SCANNED_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".pyi",
    ".md", ".rst", ".txt",
    ".json", ".jsonl",
    ".yaml", ".yml",
    ".toml", ".cfg", ".ini",
    ".sql",
    ".sh", ".bash", ".zsh", ".ps1",
    ".env",  # only matched if accidentally staged; gitignored by default
    ".csv", ".tsv",
    ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx",
})

MAX_FILE_BYTES: int = 10 * 1024 * 1024       # 10 MB
BINARY_SNIFF_BYTES: int = 8 * 1024           # check first 8 KB for NUL


def staged_files() -> list[Path]:
    """Return list of staged file paths (added/copied/modified/renamed)."""
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(line) for line in out.stdout.splitlines() if line.strip()]


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def load_patterns(rules_files: list[Path]) -> list[tuple[Path, int, re.Pattern[str]]]:
    """Load regex patterns from one or more rule files.

    Returns a list of (rules_file, line_number, compiled_pattern) tuples so
    that violations can be traced back to the source rule.
    Missing files are skipped quietly.
    """
    patterns: list[tuple[Path, int, re.Pattern[str]]] = []
    for rules_path in rules_files:
        if not rules_path.exists():
            continue
        for lineno, raw in enumerate(rules_path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                patterns.append((rules_path, lineno, re.compile(stripped, re.MULTILINE)))
            except re.error as exc:
                print(
                    f"  WARN: {rules_path}:{lineno} — invalid regex `{stripped}`: {exc}",
                    file=sys.stderr,
                )
    return patterns


def scan_file(
    path: Path,
    patterns: list[tuple[Path, int, re.Pattern[str]]],
) -> list[tuple[Path, int, re.Pattern[str], str]]:
    """Return list of (rules_file, lineno, pattern, matched_text) hits."""
    hits: list[tuple[Path, int, re.Pattern[str], str]] = []

    if path.suffix.lower() not in SCANNED_EXTENSIONS:
        return hits
    try:
        size = path.stat().st_size
    except OSError:
        return hits
    if size > MAX_FILE_BYTES:
        return hits

    try:
        raw = path.read_bytes()
    except OSError:
        return hits
    if looks_binary(raw):
        return hits

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    for rules_file, lineno, pattern in patterns:
        match = pattern.search(text)
        if match:
            hits.append((rules_file, lineno, pattern, match.group(0)))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-commit pattern scanner")
    parser.add_argument(
        "--label",
        required=True,
        help="Display label for the scan (e.g. 'PII', 'SECRETS')",
    )
    parser.add_argument(
        "--rules",
        action="append",
        required=True,
        type=Path,
        help="Path to a rules file (repeatable; missing optional rules are skipped)",
    )
    args = parser.parse_args()

    patterns = load_patterns(args.rules)
    if not patterns:
        # No active rules; treat as pass.
        return 0

    files = staged_files()
    if not files:
        return 0

    all_hits: list[tuple[Path, list[tuple[Path, int, re.Pattern[str], str]]]] = []
    for path in files:
        if not path.exists():
            continue
        hits = scan_file(path, patterns)
        if hits:
            all_hits.append((path, hits))

    if not all_hits:
        return 0

    # Render report.
    print()
    print(f"  [{args.label}] commit BLOCKED — staged content matches forbidden patterns:")
    print()
    for path, hits in all_hits:
        print(f"    {path}")
        for rules_file, lineno, _pattern, matched in hits:
            preview = matched if len(matched) <= 80 else matched[:77] + "..."
            print(f"      - {rules_file}:{lineno}  matched: {preview!r}")
        print()
    print("  To bypass (rare, documented in CONTRIBUTING.md):")
    print("      git commit --no-verify")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
