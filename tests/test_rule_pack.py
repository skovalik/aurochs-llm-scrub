"""Unit tests for core.rule_pack — TOML loader + register_pack."""

from __future__ import annotations

from pathlib import Path

import pytest

from aurochs_llm_scrub.core import db_connect
from aurochs_llm_scrub.core.db import MigrationRunner
from aurochs_llm_scrub.core.rule_pack import (
    RulePackLoadError,
    get_default_pack_path,
    list_packs,
    load_pack,
    register_default_pack,
    register_pack,
)


@pytest.fixture
def runs_db(tmp_path: Path) -> Path:
    """Return a freshly migrated runs.db inside tmp_path."""
    db = tmp_path / "runs.db"
    MigrationRunner(db).run()
    return db


# ---------------------------------------------------------------------------
# Bundled default pack
# ---------------------------------------------------------------------------


class TestDefaultPack:
    def test_default_pack_loads(self) -> None:
        path = get_default_pack_path()
        assert path.exists()
        pack, rules = load_pack(path)
        assert pack.name == "default"
        assert pack.version == "0.1.0"
        assert pack.schema_version == "0.1.0"
        assert len(pack.sha256) == 64
        # Bundled pack has at least the locked starter set
        # (em-dash, &mdash; entity, double-space, trailing whitespace,
        # plus several P1 signposts — at least 10).
        assert len(rules) >= 10

    def test_default_pack_severity_distribution(self) -> None:
        _pack, rules = load_pack(get_default_pack_path())
        severities = {r.severity_default for r in rules}
        # Default pack must contain both P0 (mechanical) and P1
        # (signposting) severities.
        assert "P0" in severities
        assert "P1" in severities

    def test_default_pack_has_mechanical_and_english_rules(self) -> None:
        _pack, rules = load_pack(get_default_pack_path())
        # Some rules are language=None (mechanical), others language="en".
        langs = {r.language for r in rules}
        assert None in langs
        assert "en" in langs


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_rejects_missing_name(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            'version = "0.1.0"\n'
            "[[rule]]\n"
            'rule_id = "x"\n'
            'name = "x"\n'
            'severity = "P0"\n'
            'pattern = "x"\n'
            'description = "x"\n',
            encoding="utf-8",
        )
        with pytest.raises(RulePackLoadError, match="missing or empty 'name'"):
            load_pack(bad)

    def test_rejects_missing_version(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            'name = "x"\n'
            "[[rule]]\n"
            'rule_id = "x"\n'
            'name = "x"\n'
            'severity = "P0"\n'
            'pattern = "x"\n'
            'description = "x"\n',
            encoding="utf-8",
        )
        with pytest.raises(
            RulePackLoadError, match="missing or empty 'version'"
        ):
            load_pack(bad)

    def test_rejects_empty_rule_list(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            'name = "x"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        with pytest.raises(
            RulePackLoadError, match="must contain at least one"
        ):
            load_pack(bad)

    def test_rejects_invalid_severity(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            'name = "x"\nversion = "0.1.0"\n'
            "[[rule]]\n"
            'rule_id = "x"\n'
            'name = "x"\n'
            'severity = "P9"\n'
            'pattern = "x"\n'
            'description = "x"\n',
            encoding="utf-8",
        )
        with pytest.raises(RulePackLoadError, match="severity must be"):
            load_pack(bad)

    def test_rejects_duplicate_rule_id(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            'name = "x"\nversion = "0.1.0"\n'
            "[[rule]]\n"
            'rule_id = "dup"\n'
            'name = "first"\n'
            'severity = "P0"\n'
            'pattern = "a"\n'
            'description = "x"\n'
            "[[rule]]\n"
            'rule_id = "dup"\n'
            'name = "second"\n'
            'severity = "P0"\n'
            'pattern = "b"\n'
            'description = "x"\n',
            encoding="utf-8",
        )
        with pytest.raises(RulePackLoadError, match="duplicate rule_id"):
            load_pack(bad)

    def test_rejects_null_byte_in_pattern(self, tmp_path: Path) -> None:
        # BLOCKER #7: null-byte rejection at load time.
        bad = tmp_path / "bad.toml"
        bad.write_text(
            'name = "x"\nversion = "0.1.0"\n'
            "[[rule]]\n"
            'rule_id = "nul"\n'
            'name = "n"\n'
            'severity = "P0"\n'
            'pattern = "before\\u0000after"\n'
            'description = "x"\n',
            encoding="utf-8",
        )
        with pytest.raises(
            RulePackLoadError, match=r"pattern contains null byte"
        ):
            load_pack(bad)

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("this is not [valid TOML at all\n", encoding="utf-8")
        with pytest.raises(RulePackLoadError, match="failed to parse"):
            load_pack(bad)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_pack(tmp_path / "does-not-exist.toml")

    def test_language_empty_string_means_none(self, tmp_path: Path) -> None:
        # plan v4: language = "" in TOML becomes None in the dataclass.
        good = tmp_path / "g.toml"
        good.write_text(
            'name = "x"\nversion = "0.1.0"\n'
            "[[rule]]\n"
            'rule_id = "mech"\n'
            'name = "n"\n'
            'severity = "P0"\n'
            'language = ""\n'
            'pattern = "x"\n'
            'description = "d"\n',
            encoding="utf-8",
        )
        _pack, rules = load_pack(good)
        assert rules[0].language is None


# ---------------------------------------------------------------------------
# list_packs
# ---------------------------------------------------------------------------


class TestListPacks:
    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        out = list_packs(tmp_path)
        assert out == []

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        out = list_packs(tmp_path / "nope")
        assert out == []

    def test_enumerates_toml_files(self, tmp_path: Path) -> None:
        for i, name in enumerate(("alpha", "beta")):
            (tmp_path / f"{name}.toml").write_text(
                f'name = "{name}"\nversion = "0.{i}.0"\n'
                "[[rule]]\n"
                f'rule_id = "{name}-1"\n'
                f'name = "{name}"\n'
                'severity = "P0"\n'
                'pattern = "x"\n'
                'description = "d"\n',
                encoding="utf-8",
            )
        packs = list_packs(tmp_path)
        names = {p.name for p in packs}
        assert names == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# register_pack — DB persistence
# ---------------------------------------------------------------------------


class TestRegisterPack:
    def test_register_default_idempotent(self, runs_db: Path) -> None:
        pack_id_1, rule_ids_1 = register_default_pack(runs_db)
        pack_id_2, rule_ids_2 = register_default_pack(runs_db)
        assert pack_id_1 == pack_id_2
        assert rule_ids_1 == rule_ids_2

    def test_register_persists_rows(self, runs_db: Path) -> None:
        pack_id, rule_ids = register_default_pack(runs_db)
        conn = db_connect(runs_db)
        try:
            row = conn.execute(
                "SELECT name, version FROM rule_packs WHERE id = ?",
                (pack_id,),
            ).fetchone()
            assert row["name"] == "default"
            count = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE pack_id = ?",
                (pack_id,),
            ).fetchone()[0]
            assert count == len(rule_ids)
        finally:
            conn.close()

    def test_two_packs_different_versions(
        self, runs_db: Path, tmp_path: Path
    ) -> None:
        # Register the default pack, then a custom pack named "default"
        # at a different version. Both rows should coexist.
        register_default_pack(runs_db)
        custom = tmp_path / "custom.toml"
        custom.write_text(
            'name = "default"\nversion = "0.2.0"\n'
            "[[rule]]\n"
            'rule_id = "x"\n'
            'name = "x"\n'
            'severity = "P0"\n'
            'pattern = "x"\n'
            'description = "d"\n',
            encoding="utf-8",
        )
        pack, rules = load_pack(custom)
        register_pack(runs_db, pack, rules)

        conn = db_connect(runs_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM rule_packs WHERE name = 'default'"
            ).fetchone()[0]
            assert count == 2
        finally:
            conn.close()
