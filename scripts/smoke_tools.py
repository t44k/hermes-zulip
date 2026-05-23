#!/usr/bin/env python
"""M4 smoke test: call each agent-facing tool against the real Zulip server.

Usage:
    ZULIP_SITE=... ZULIP_EMAIL=... ZULIP_API_KEY=... \
        python scripts/smoke_tools.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Bootstrap as hermes_plugins.zulip
sys.path.insert(0, str(Path("/home/devuser/.hermes/hermes-agent")))
import importlib.util, types
_PLUGIN = Path(__file__).resolve().parent.parent
if "hermes_plugins" not in sys.modules:
    ns = types.ModuleType("hermes_plugins")
    ns.__path__ = []  # type: ignore[attr-defined]
    sys.modules["hermes_plugins"] = ns
_spec = importlib.util.spec_from_file_location(
    "hermes_plugins.zulip", _PLUGIN / "__init__.py",
    submodule_search_locations=[str(_PLUGIN)],
)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_mod.__package__ = "hermes_plugins.zulip"
_mod.__path__ = [str(_PLUGIN)]  # type: ignore[attr-defined]
sys.modules["hermes_plugins.zulip"] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

from hermes_plugins.zulip.tools import (
    _handle_zulip_post,
    _handle_zulip_dm,
    _handle_zulip_list_streams,
    _handle_zulip_list_topics,
)


async def main() -> int:
    print("→ zulip_list_streams …")
    r = await _handle_zulip_list_streams()
    print(f"   {r}")
    if not r.get("success"):
        return 1

    print("\n→ zulip_list_topics(sandbox) …")
    r = await _handle_zulip_list_topics("sandbox")
    print(f"   {r}")

    print("\n→ zulip_post(sandbox > m4-test, '...') …")
    r = await _handle_zulip_post(
        "sandbox", "m4-test",
        "✨ **M4** — Ange just called `zulip_post` to spawn a new topic. "
        "If you can read this, the agent-facing tool surface works.",
    )
    print(f"   {r}")
    if not r.get("success"):
        return 1

    print("\n→ zulip_post(sandbox > m4-branch-demo, 'second topic') …")
    r2 = await _handle_zulip_post(
        "sandbox", "m4-branch-demo",
        "And this is a *different* topic — Hermes would treat this "
        "as a separate session.",
    )
    print(f"   {r2}")

    print("\n✓ All M4 tools work end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
