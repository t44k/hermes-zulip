#!/usr/bin/env python
"""M2 smoke test: connect, register an event queue, listen for messages.

Run this then send a message to the bot in Zulip (DM or @-mention in a
subscribed stream). The script will print events as they arrive, then exit
after the first dispatched message — or after --timeout seconds, whichever
comes first.

Usage:
    ZULIP_SITE=... ZULIP_EMAIL=... ZULIP_API_KEY=... \
        python scripts/smoke_listen.py [--timeout 120]
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Bootstrap: load adapter as hermes_plugins.zulip
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

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import platform_registry, PlatformEntry  # noqa: E402
from hermes_plugins.zulip.adapter import ZulipAdapter, register  # noqa: E402


class _Ctx:
    def register_platform(self, name, label, adapter_factory, check_fn,
                          validate_config=None, required_env=None,
                          install_hint="", **kw):
        kw.pop("plugin_name", None)
        platform_registry.register(PlatformEntry(
            name=name, label=label, adapter_factory=adapter_factory,
            check_fn=check_fn, validate_config=validate_config,
            required_env=required_env or [], install_hint=install_hint,
            source="plugin", **kw,
        ))


if not platform_registry.is_registered("zulip"):
    register(_Ctx())


async def main(timeout: float) -> int:
    site = os.getenv("ZULIP_SITE")
    email = os.getenv("ZULIP_EMAIL")
    api_key = os.getenv("ZULIP_API_KEY")
    if not (site and email and api_key):
        print("missing ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY", file=sys.stderr)
        return 2

    cfg = PlatformConfig(enabled=True, extra={
        "site": site, "email": email, "api_key": api_key,
        "verify_tls": os.getenv("ZULIP_VERIFY_TLS", "true"),
    })

    seen: list = []
    a = ZulipAdapter(cfg)

    async def _capture(event):
        src = event.source
        thread = f" > {src.thread_id}" if src.thread_id else ""
        print(f"\n📨 [{src.chat_id}{thread}] {src.user_name}: {event.text!r}")
        seen.append(event)

    a.handle_message = _capture  # type: ignore[method-assign]

    ok = await a.connect()
    if not ok:
        print("connect() failed", file=sys.stderr)
        return 1
    print(f"✓ Connected. Listening for {int(timeout)}s …")
    print(f"  Bot user_id={a._me.get('user_id')}  subscribed_streams={list(a._streams_by_name)}")
    print("  → Send the bot a message from Zulip now.")

    try:
        # Wait the full timeout, collecting as many messages as arrive.
        await asyncio.wait_for(asyncio.Event().wait(), timeout=timeout)
        rc = 0
    except asyncio.TimeoutError:
        if seen:
            print(f"\n✓ Window closed. Got {len(seen)} message(s).")
            rc = 0
        else:
            print(f"\n× Timed out after {int(timeout)}s with no messages.")
            rc = 1
    finally:
        await a.disconnect()
    return rc


if __name__ == "__main__":
    t = 120.0
    if "--timeout" in sys.argv:
        i = sys.argv.index("--timeout")
        t = float(sys.argv[i + 1])
    sys.exit(asyncio.run(main(t)))
