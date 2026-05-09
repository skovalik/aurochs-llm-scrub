"""aurochs-llm-scrub MCP server package.

Exposes scrub tools to other Claude Code sessions and MCP clients via the
Model Context Protocol stdio transport.

Local files: ``scrub_server.py``, ``schemas.py``.

SDK access: ``import mcp`` / ``from mcp.server.lowlevel import Server`` —
the SDK is unambiguously the installed package; this local namespace is
``aurochs_llm_scrub.mcp``. Naming the local module ``scrub_server.py``
(not ``server.py``) avoids any appearance of shadowing the SDK's
``mcp.server`` package.
"""

from __future__ import annotations

import importlib.util as _importlib_util


def _sdk_available() -> bool:
    """Return True iff the installed Anthropic ``mcp`` SDK can be imported."""
    return (
        _importlib_util.find_spec("mcp.types") is not None
        and _importlib_util.find_spec("mcp.server.lowlevel") is not None
    )


__all__ = ["_sdk_available"]
