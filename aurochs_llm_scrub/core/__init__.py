"""aurochs-llm-scrub core library — public spine.

Pipeline + CLI + MCP surfaces import their data layer from here. The
re-export surface below is the contract: changes to it are
binary-compat changes for downstream consumers, including
aurochs-voice's overlay layer.

The first block re-exports the foundational types + DB helpers (these
have no inter-module dependencies). The second block re-exports the
pipeline / redact / precommit / recovery / rule_pack / scrubber surface
so CLI consumers can write::

    from aurochs_llm_scrub.core import scan, scrub, ScanResult, verify

The pipeline modules import the foundational types eagerly, so the
re-exports below are safe — Python's module-cache idempotency means a
nested ``aurochs_llm_scrub.core.scrubber`` import at this module's
import-time resolves cleanly without the dreaded "partially initialized
module" trap.
"""

# Import order is LOAD-BEARING here. The pipeline modules below
# (precommit/recovery/redact/rule_pack/scrubber) all do
# ``from aurochs_llm_scrub.core import (Rule, ScrubMode, ...)`` at
# their own import time, which means those names MUST already be bound
# in this module's namespace when the pipeline imports execute. We
# therefore import the foundational types FIRST, then the pipeline
# modules. ruff's import sorter would alphabetize this and break the
# graph; the ``isort: split`` markers preserve the deliberate order.
from __future__ import annotations

from aurochs_core import LockError, MigrateLock, WriteLock

from aurochs_llm_scrub.core.db import MigrationError, MigrationRunner, db_connect
from aurochs_llm_scrub.core.types import (
    HASH_INPUT_VERSION,
    DisabledRuleEntry,
    Finding,
    Rule,
    RuleMetrics,
    RulePack,
    ScrubMode,
    ScrubRun,
    ScrubStatus,
    compute_finding_sha256,
    normalize_text,
)

# isort: split
# Pipeline + tooling re-exports — see import-order note above.

from aurochs_llm_scrub.core.precommit import install as install_precommit
from aurochs_llm_scrub.core.precommit import is_installed as is_precommit_installed
from aurochs_llm_scrub.core.precommit import run_precommit
from aurochs_llm_scrub.core.recovery import VerifyReport, verify
from aurochs_llm_scrub.core.redact import apply_redactions, parse_pragma_ranges
from aurochs_llm_scrub.core.rule_pack import (
    get_default_pack_path,
    load_pack,
    register_default_pack,
)
from aurochs_llm_scrub.core.scrubber import (
    ScanResult,
    scan,
    scan_text,
    scan_text_report,
    scrub,
    scrub_text_report,
)

__all__ = [
    "HASH_INPUT_VERSION",
    "DisabledRuleEntry",
    "Finding",
    "LockError",
    "MigrateLock",
    "MigrationError",
    "MigrationRunner",
    "Rule",
    "RuleMetrics",
    "RulePack",
    "ScanResult",
    "ScrubMode",
    "ScrubRun",
    "ScrubStatus",
    "VerifyReport",
    "WriteLock",
    "apply_redactions",
    "compute_finding_sha256",
    "db_connect",
    "get_default_pack_path",
    "install_precommit",
    "is_precommit_installed",
    "load_pack",
    "normalize_text",
    "parse_pragma_ranges",
    "register_default_pack",
    "run_precommit",
    "scan",
    "scan_text",
    "scan_text_report",
    "scrub",
    "scrub_text_report",
    "verify",
]
