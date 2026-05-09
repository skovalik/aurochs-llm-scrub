"""TOML rule pack loader.

A rule pack is a TOML file describing a name + version + list of rules.
Loading a pack returns a ``RulePack`` dataclass plus a list of unbound
``Rule`` dataclasses (``pack_id`` is set when ``register_pack`` inserts
them). The bundled default pack lives at ``core/default_rules.toml``.

Plan v4 contract:

* **BLOCKER #7 — null-byte rejection.** Any rule pattern containing a
  raw ``\\x00`` byte is rejected at load time. Custom patterns are
  limited to printable + standard escape characters; the load step is
  the canonical rejection point so the rule never reaches the engine.
* **Strict schema validation.** Unknown top-level fields and unknown
  per-rule fields are tolerated (forward-compat with future schema
  bumps) but a structurally-invalid pack — missing ``name``, missing
  ``version``, an empty rule list, a rule with empty ``rule_id`` /
  empty ``pattern`` / unknown severity — raises
  ``RulePackLoadError``.
* **SHA-256 of the canonical pack bytes** used for drift detection. We
  hash the on-disk file bytes (not the parsed dict) so every byte
  written is what gets hashed. Loading the in-memory canonical
  serialization is for the bundled default pack only (which is shipped
  as a ``.toml`` file too, so it has bytes either way).
* **Pack-namespacing** of rule_id collisions across packs is enforced
  at the SQL layer; this loader does not check across-pack uniqueness
  because packs are loaded in isolation.
* **Snapshot-at-init** is the caller's responsibility — once a pack
  has been loaded, the returned ``(RulePack, list[Rule])`` tuple is the
  immutable runtime view; the loader does not re-read the file.

The TOML schema (v0.1.0)::

    schema_version = "0.1.0"          # optional; defaults to 0.1.0
    name = "default"                  # required; pack identifier
    version = "0.1.0"                 # required; semver-style

    [[rule]]
    rule_id = "p0-em-dash"            # required; pack-stable id
    name = "Em dash"                  # required; human label
    severity = "P0"                   # required; P0/P1/P2
    language = ""                     # optional; "" = mechanical, "en"/...
    pattern = "—"                     # required; regex string
    description = "..."               # required; one-line rationale

Note that the TOML loader treats ``language = ""`` as the
"language-independent / always-on" sentinel; the dataclass uses ``None``
for the same concept. The conversion happens here.
"""

from __future__ import annotations

import hashlib
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aurochs_llm_scrub.core import (
    RulePack,
    WriteLock,
    db_connect,
)


@dataclass(slots=True, kw_only=True)
class _PendingRule:
    """Rule fields parsed from TOML, before a pack_id is assigned.

    We can't construct a real ``Rule`` until ``register_pack`` has
    inserted the pack and obtained its SQLite-assigned id (the dataclass
    enforces ``pack_id > 0``). This intermediate carries the parsed
    fields with the same shape so the loader's surface stays
    type-stable.
    """

    rule_id: str
    name: str
    pattern: str
    severity_default: str
    language: str | None
    description: str

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class RulePackLoadError(ValueError):
    """Raised when a TOML rule pack fails strict-schema validation."""


# ---------------------------------------------------------------------------
# Default-pack location (bundled with the package)
# ---------------------------------------------------------------------------


_DEFAULT_PACK_PATH: Path = Path(__file__).parent / "default_rules.toml"


def get_default_pack_path() -> Path:
    """Return the absolute filesystem path of the bundled default pack.

    The path always exists in a normal install; the loader's tests
    assert it. Useful for CLI ``llm-scrub rules show --pack default``
    output.
    """
    if not _DEFAULT_PACK_PATH.exists():
        raise FileNotFoundError(
            f"Bundled default rule pack missing at {_DEFAULT_PACK_PATH}. "
            "Package install may be corrupt."
        )
    return _DEFAULT_PACK_PATH


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_pack(pack_path: Path) -> tuple[RulePack, list[_PendingRule]]:
    """Parse a TOML rule pack file and return ``(RulePack, [_PendingRule, ...])``.

    The returned ``RulePack`` carries the file's content sha256 + the
    parsed name/version. The pending-rule list is the per-rule fields
    with no ``pack_id`` yet — ``register_pack`` is the only path that
    converts them into ``Rule`` rows because the SQLite-assigned pack
    id is the FK ``Rule.pack_id`` requires (the dataclass enforces
    ``pack_id > 0``).

    Parameters
    ----------
    pack_path:
        Path to the ``.toml`` file. Must exist. May be the bundled
        default pack or a user-authored custom pack under
        ``~/.aurochs-llm-scrub/``.

    Returns
    -------
    tuple[RulePack, list[Rule]]
        Pack metadata + rule rows ready to be re-keyed and inserted.

    Raises
    ------
    FileNotFoundError
        If ``pack_path`` does not exist.
    RulePackLoadError
        If the file is malformed TOML, fails schema validation, or
        contains a null-byte in any pattern (BLOCKER #7).
    """
    if not pack_path.exists():
        raise FileNotFoundError(f"rule pack file not found: {pack_path}")

    raw_bytes = pack_path.read_bytes()
    sha = hashlib.sha256(raw_bytes).hexdigest()

    try:
        parsed: dict[str, Any] = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise RulePackLoadError(
            f"failed to parse {pack_path}: {exc}"
        ) from exc

    name = parsed.get("name")
    version = parsed.get("version")
    schema_version = parsed.get("schema_version", "0.1.0")
    rules_raw = parsed.get("rule", [])

    if not isinstance(name, str) or not name:
        raise RulePackLoadError(
            f"{pack_path}: missing or empty 'name' (top-level field)"
        )
    if not isinstance(version, str) or not version:
        raise RulePackLoadError(
            f"{pack_path}: missing or empty 'version' (top-level field)"
        )
    if not isinstance(schema_version, str) or not schema_version:
        raise RulePackLoadError(
            f"{pack_path}: 'schema_version' must be non-empty string"
        )
    if not isinstance(rules_raw, list) or not rules_raw:
        raise RulePackLoadError(
            f"{pack_path}: pack must contain at least one [[rule]] table"
        )

    pack = RulePack(
        name=name,
        version=version,
        schema_version=schema_version,
        source_path=str(pack_path),
        sha256=sha,
        registered_at=int(time.time()),
    )

    rules: list[_PendingRule] = []
    seen_ids: set[str] = set()
    for idx, entry in enumerate(rules_raw):
        rule = _build_rule_from_entry(
            entry,
            pack_path=pack_path,
            entry_index=idx,
        )
        if rule.rule_id in seen_ids:
            raise RulePackLoadError(
                f"{pack_path}: duplicate rule_id={rule.rule_id!r} "
                f"in pack (entry index {idx})"
            )
        seen_ids.add(rule.rule_id)
        rules.append(rule)

    return pack, rules


def _build_rule_from_entry(
    entry: Any,
    *,
    pack_path: Path,
    entry_index: int,
) -> _PendingRule:
    """Validate and convert one TOML ``[[rule]]`` table to ``_PendingRule``."""
    if not isinstance(entry, dict):
        raise RulePackLoadError(
            f"{pack_path}: rule entry {entry_index} must be a TOML table, "
            f"got {type(entry).__name__}"
        )

    rule_id = entry.get("rule_id")
    name = entry.get("name")
    severity = entry.get("severity")
    pattern = entry.get("pattern")
    description = entry.get("description")
    language_raw = entry.get("language", "")

    if not isinstance(rule_id, str) or not rule_id:
        raise RulePackLoadError(
            f"{pack_path}: rule entry {entry_index} missing 'rule_id'"
        )
    if not isinstance(name, str) or not name:
        raise RulePackLoadError(
            f"{pack_path}: rule {rule_id!r} missing 'name'"
        )
    if not isinstance(severity, str) or severity not in {"P0", "P1", "P2"}:
        raise RulePackLoadError(
            f"{pack_path}: rule {rule_id!r} severity must be P0/P1/P2, "
            f"got {severity!r}"
        )
    if not isinstance(pattern, str) or not pattern:
        raise RulePackLoadError(
            f"{pack_path}: rule {rule_id!r} missing 'pattern'"
        )
    if "\x00" in pattern:
        # BLOCKER #7: reject null-byte patterns at load time.
        raise RulePackLoadError(
            f"{pack_path}: rule {rule_id!r} pattern contains null byte "
            "(\\x00) — rejected per plan v4 BLOCKER #7"
        )
    if not isinstance(description, str) or not description:
        raise RulePackLoadError(
            f"{pack_path}: rule {rule_id!r} missing 'description'"
        )
    if not isinstance(language_raw, str):
        raise RulePackLoadError(
            f"{pack_path}: rule {rule_id!r} 'language' must be a string "
            f"(use \"\" for language-independent), got "
            f"{type(language_raw).__name__}"
        )

    # "" → None sentinel internally (mechanical / always-on).
    language: str | None = language_raw or None

    return _PendingRule(
        rule_id=rule_id,
        name=name,
        pattern=pattern,
        severity_default=severity,
        language=language,
        description=description,
    )


# ---------------------------------------------------------------------------
# list_packs — directory enumeration
# ---------------------------------------------------------------------------


def list_packs(packs_dir: Path) -> list[RulePack]:
    """Enumerate ``*.toml`` rule packs in a directory.

    Used for ``~/.aurochs-llm-scrub/`` discovery and any custom packs
    a user drops alongside the bundled default. Each file is parsed via
    ``load_pack``; malformed files raise ``RulePackLoadError`` from
    here so a partially-bad directory surfaces all errors via repeated
    calls (the caller chooses whether to keep going on first failure
    or abort).

    Parameters
    ----------
    packs_dir:
        Directory to scan. Non-existent directories return an empty
        list (rather than raise) so a fresh install with no custom
        packs does not error.

    Returns
    -------
    list[RulePack]
        Pack metadata in directory-order (sorted lexically by file
        name for stable output). Empty if the directory is missing or
        contains no ``.toml`` files.
    """
    if not packs_dir.exists():
        return []
    out: list[RulePack] = []
    for tom in sorted(packs_dir.glob("*.toml")):
        pack, _rules = load_pack(tom)
        out.append(pack)
    return out


# ---------------------------------------------------------------------------
# register_pack — persist to runs.db
# ---------------------------------------------------------------------------


def register_pack(
    db_path: Path,
    pack: RulePack,
    rules: list[_PendingRule],
) -> tuple[int, list[int]]:
    """Insert ``pack`` and ``rules`` into runs.db under the WriteLock.

    Re-registering the same ``(name, version)`` pair is a no-op via
    ``INSERT OR IGNORE`` on the UNIQUE index — the existing pack id is
    returned and rules are upserted by ``(pack_id, rule_id)``. Bumping
    the pack ``version`` and re-registering creates a new row, leaving
    historical findings still tied to the old pack via FK (per types.py
    contract).

    Parameters
    ----------
    db_path:
        Path to runs.db (will be created with WAL pragmas via
        db_connect if missing). Migration must already have been run
        (caller's responsibility — typically ``MigrationRunner(...).run()``
        before this).
    pack:
        Pack metadata returned by ``load_pack``.
    rules:
        Rule rows returned by ``load_pack``. Their ``pack_id`` is
        rewritten to the inserted pack id.

    Returns
    -------
    tuple[int, list[int]]
        ``(pack_id, [rule_id, rule_id, ...])`` — the SQLite-assigned
        primary keys, in the same order as the ``rules`` argument.

    Raises
    ------
    LockError
        If the WriteLock cannot be acquired within 30s.
    """
    with WriteLock(db_path, timeout=30.0):
        conn = db_connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Upsert the pack row; pull the id back regardless of
                # whether INSERT or IGNORE-via-existing applied.
                conn.execute(
                    "INSERT OR IGNORE INTO rule_packs "
                    "(name, version, schema_version, source_path, sha256, "
                    "registered_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        pack.name,
                        pack.version,
                        pack.schema_version,
                        pack.source_path,
                        pack.sha256,
                        pack.registered_at,
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM rule_packs WHERE name = ? AND version = ?",
                    (pack.name, pack.version),
                ).fetchone()
                if row is None:  # pragma: no cover - defensive
                    raise RuntimeError(
                        f"rule_pack INSERT OR IGNORE produced no row for "
                        f"{pack.name!r} v{pack.version!r}"
                    )
                pack_id = int(row["id"])

                rule_ids: list[int] = []
                for rule in rules:
                    # Upsert by (pack_id, rule_id). On collision the
                    # existing row stays — bumping a rule's pattern
                    # requires a new pack version per plan v4
                    # snapshot-at-init invariant.
                    conn.execute(
                        "INSERT OR IGNORE INTO rules "
                        "(pack_id, rule_id, name, pattern, severity_default, "
                        "language, description, enabled) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            pack_id,
                            rule.rule_id,
                            rule.name,
                            rule.pattern,
                            rule.severity_default,
                            rule.language,
                            rule.description,
                            1,
                        ),
                    )
                    rrow = conn.execute(
                        "SELECT id FROM rules "
                        "WHERE pack_id = ? AND rule_id = ?",
                        (pack_id, rule.rule_id),
                    ).fetchone()
                    if rrow is None:  # pragma: no cover - defensive
                        raise RuntimeError(
                            f"rule INSERT OR IGNORE produced no row for "
                            f"({pack_id}, {rule.rule_id!r})"
                        )
                    rule_ids.append(int(rrow["id"]))

                conn.execute("COMMIT")
                return pack_id, rule_ids
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Convenience: register the bundled default pack
# ---------------------------------------------------------------------------


def register_default_pack(db_path: Path) -> tuple[int, list[int]]:
    """Load + register the bundled default rule pack into runs.db.

    Idempotent — if the default pack has been registered already, the
    existing ids are returned. Used by ``llm-scrub init`` and by tests
    that need a populated rule set without authoring a custom pack.
    """
    pack, rules = load_pack(get_default_pack_path())
    return register_pack(db_path, pack, rules)


# ---------------------------------------------------------------------------
# migrate — schema-version migration stub (v0.2 lands the real impl)
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class MigrateResult:
    """Returned by :func:`migrate`. Carries source/target version pair.

    Attributes mirror the CLI's ``llm-scrub rules migrate`` output (see
    ``cli.rules_cmd._cmd_migrate``): ``from_version`` / ``to_version``
    are surfaced verbatim. Future fields (rules added, rules dropped,
    pack renames) accrete here without changing the CLI shape.
    """

    from_version: str
    to_version: str


def migrate(
    db_path: Path,
    from_version: str | None = None,
    to_version: str | None = None,
) -> MigrateResult:
    """Migrate the rule-pack schema between versions.

    v0.1 ships a single schema version (``"0.1.0"``), so the migrate
    surface is a stub: it raises :class:`NotImplementedError` to make
    the unwired path explicit. The CLI's ``rules_cmd._cmd_migrate``
    catches ImportError + falls back to a clear stderr message; this
    NotImplementedError surface gives the same clarity at runtime
    without the CLI losing the lazy-import path.

    Schema migrations land in v0.2 along with the first pack-format
    bump (planned: per-rule ``auto_fix`` strings + ``pattern_type``
    discrimination between regex and literal).

    Parameters
    ----------
    db_path:
        Path to runs.db. Migrations re-register the default pack
        + invoke per-version helpers; the lock model is identical to
        :func:`register_pack`.
    from_version:
        Source schema version; ``None`` triggers detection (read the
        max ``rule_packs.schema_version``).
    to_version:
        Target schema version; ``None`` selects the latest version
        bundled with this release.

    Raises
    ------
    NotImplementedError
        Always, in v0.1.
    """
    _ = (db_path, from_version, to_version)
    raise NotImplementedError(
        "rule-pack schema migrations land in v0.2; "
        "v0.1 has only one schema version (0.1.0)."
    )


__all__ = [
    "MigrateResult",
    "RulePackLoadError",
    "_PendingRule",
    "get_default_pack_path",
    "list_packs",
    "load_pack",
    "migrate",
    "register_default_pack",
    "register_pack",
]
