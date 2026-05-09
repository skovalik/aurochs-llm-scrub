"""Schema migrations for aurochs-llm-scrub.

T0 ships only ``0001_initial.sql``. Future versions add files named
``000N_description.sql`` and the runner discovers them by glob. The
runner enforces sequential application, single-writer via
``MigrateLock``, and records ``schema_version`` rows with a status
field so partial migrations can be detected on the next run.
"""

from __future__ import annotations
