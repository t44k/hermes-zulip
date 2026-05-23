#!/usr/bin/env python
"""M1 unit tests — runs without pytest, no network required.

The Hermes plugin loader handles relative imports via spec_from_file_location
with submodule_search_locations. Outside that loader (e.g. plain pytest run
from the repo root) those relative imports fail, so we replicate the loader
machinery here in a single self-contained script.

Usage:
    PYTHONPATH=/path/to/hermes-agent python tests/run_tests.py
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path


def _load_plugin() -> object:
    """Mimic hermes_cli/plugins.py:_load_directory_module()."""
    plugin_dir = Path(__file__).resolve().parent.parent
    init_file = plugin_dir / "__init__.py"
    module_name = "hermes_plugins.zulip"

    # Ensure namespace parent
    import types as _types
    if "hermes_plugins" not in sys.modules:
        ns = _types.ModuleType("hermes_plugins")
        ns.__path__ = []  # type: ignore[attr-defined]
        ns.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = ns

    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(plugin_dir)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #

_failures = 0
_passes = 0


def _check(name: str, cond: bool, detail: str = "") -> None:
    global _failures, _passes
    if cond:
        _passes += 1
        print(f"  ✓ {name}")
    else:
        _failures += 1
        print(f"  ✗ {name}  {detail}")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def run() -> int:
    print("Loading plugin …")
    try:
        pkg = _load_plugin()
    except Exception:
        traceback.print_exc()
        print("FAIL: plugin failed to import")
        return 1
    print(f"  ✓ plugin imported ({pkg})")

    from hermes_plugins.zulip.adapter import (
        PLATFORM_HINT, PLATFORM_KEY, _parse_chat_id, _truthy,
        check_requirements, validate_config, ZulipAdapter,
    )
    from hermes_plugins.zulip.client import ZulipAPIError, ZulipClient
    from gateway.config import PlatformConfig

    print("\nchat_id parsing")
    _check("stream:sandbox", _parse_chat_id("stream:sandbox") == ("stream", "sandbox"))
    _check("dm:a@x,b@y",
           _parse_chat_id("dm:a@x.com,b@y.com") == ("dm", "a@x.com,b@y.com"))
    _check("bare → stream", _parse_chat_id("sandbox") == ("stream", "sandbox"))
    _check("unknown prefix → stream-whole",
           _parse_chat_id("weird:thing") == ("stream", "weird:thing"))

    print("\ntruthy parser")
    for v in ("1", "true", "TRUE", "Yes", "on", "y"):
        _check(f"truthy({v!r})", _truthy(v) is True)
    for v in ("0", "false", "no", "", "off", None):
        _check(f"!truthy({v!r})", _truthy(v) is False)

    print("\nplatform metadata")
    _check("PLATFORM_KEY == zulip", PLATFORM_KEY == "zulip")
    _check("hint mentions streams", "stream" in PLATFORM_HINT.lower())
    _check("hint mentions topics", "topic" in PLATFORM_HINT.lower())
    _check("check_requirements() True (httpx installed)", check_requirements() is True)

    print("\nvalidate_config")
    cfg = PlatformConfig(enabled=True, extra={})
    ok, msg = validate_config(cfg)
    _check("empty config rejected", not ok and "ZULIP_SITE" in msg)
    cfg2 = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })
    ok2, msg2 = validate_config(cfg2)
    _check("full config accepted", ok2 is True, msg2)

    print("\nZulipAPIError")
    e = ZulipAPIError(400, "BAD_REQUEST", "boom", raw={"x": 1})
    _check("error.status", e.status == 400)
    _check("error.code", e.code == "BAD_REQUEST")
    _check("error str", "boom" in str(e))
    _check("error.raw", e.raw == {"x": 1})

    print("\nZulipClient construction (no network)")
    c = ZulipClient("https://zulip.example.com", "ange-bot@example.com", "k")
    _check("base_url", c.base_url == "https://zulip.example.com/api/v1")
    _check("trailing slash stripped",
           ZulipClient("https://zulip.example.com/", "e", "k").base_url
           == "https://zulip.example.com/api/v1")
    _check("scheme prepended",
           ZulipClient("zulip.example.com", "e", "k").base_url
           == "https://zulip.example.com/api/v1")

    print("\nZulipAdapter construction (no network)")
    # Register with platform_registry first so Platform("zulip") resolves.
    from gateway.platform_registry import platform_registry, PlatformEntry
    if not platform_registry.is_registered("zulip"):
        class _Ctx:
            def register_platform(self, name, label, adapter_factory, check_fn,
                                  validate_config=None, required_env=None,
                                  install_hint="", **kw):
                kw.pop("plugin_name", None)
                platform_registry.register(PlatformEntry(
                    name=name, label=label,
                    adapter_factory=adapter_factory,
                    check_fn=check_fn,
                    validate_config=validate_config,
                    required_env=required_env or [],
                    install_hint=install_hint,
                    source="plugin",
                    **kw,
                ))
        from hermes_plugins.zulip.adapter import register as _reg
        _reg(_Ctx())
    _check("platform_registry has zulip", platform_registry.is_registered("zulip"))

    cfg3 = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })
    a = ZulipAdapter(cfg3)
    _check("adapter.site", a.site == "https://zulip.example.com")
    _check("adapter.email", a.email == "ange-bot@example.com")
    _check("adapter.api_key", a.api_key == "k")
    _check("adapter.auto_create_topics default True", a.auto_create_topics is True)
    _check("adapter.verify_tls default True", a.verify_tls is True)

    print(f"\n{_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


# --------------------------------------------------------------------------- #
# M2 tests: event dispatch
# --------------------------------------------------------------------------- #

def run_m2() -> int:
    """Run after run() — assumes plugin already loaded."""
    import asyncio
    from gateway.config import PlatformConfig
    from hermes_plugins.zulip.adapter import ZulipAdapter, DEFAULT_TOPIC

    print("\nM2: message event dispatch")

    cfg = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })
    a = ZulipAdapter(cfg)
    a._me = {"user_id": 11, "email": "ange-bot@example.com", "full_name": "Ange"}
    a._streams_by_id = {7: {"name": "sandbox", "stream_id": 7, "description": "Test playground"}}
    a._streams_by_name = {"sandbox": a._streams_by_id[7]}

    dispatched: list = []

    async def _capture(event):
        dispatched.append(event)

    # Patch handle_message to capture
    a.handle_message = _capture  # type: ignore[method-assign]

    # 1) Stream message from another user
    asyncio.run(a._handle_message_event({
        "id": 100,
        "type": "stream",
        "sender_id": 42,
        "sender_email": "tamas@359.wtf",
        "sender_full_name": "Tamas",
        "display_recipient": "sandbox",
        "subject": "auth-bug",
        "stream_id": 7,
        "content": "Hey Ange, the OAuth flow is broken.",
        "timestamp": 1234567890,
    }))
    _check("dispatched 1 event", len(dispatched) == 1)
    ev1 = dispatched[0]
    _check("ev1.text", "OAuth" in ev1.text)
    _check("ev1.chat_id = stream:sandbox", ev1.source.chat_id == "stream:sandbox")
    _check("ev1.thread_id = auth-bug", ev1.source.thread_id == "auth-bug")
    _check("ev1.parent_chat_id = stream:sandbox",
           ev1.source.parent_chat_id == "stream:sandbox")
    _check("ev1.chat_type = channel", ev1.source.chat_type == "channel")
    _check("ev1.chat_topic = stream description",
           ev1.source.chat_topic == "Test playground")
    _check("ev1.user_name = Tamas", ev1.source.user_name == "Tamas")
    _check("ev1.message_id = 100", ev1.source.message_id == "100")

    # 2) Stream message with empty topic → DEFAULT_TOPIC
    dispatched.clear()
    asyncio.run(a._handle_message_event({
        "id": 101, "type": "stream", "sender_id": 42,
        "sender_full_name": "Tamas", "display_recipient": "sandbox",
        "subject": "   ", "stream_id": 7, "content": "no topic test",
    }))
    _check("empty topic → DEFAULT_TOPIC",
           dispatched[0].source.thread_id == DEFAULT_TOPIC)

    # 3) Self message → filtered out
    dispatched.clear()
    asyncio.run(a._handle_message_event({
        "id": 102, "type": "stream", "sender_id": 11,  # ← bot's own user_id
        "sender_full_name": "Ange", "display_recipient": "sandbox",
        "subject": "auth-bug", "stream_id": 7, "content": "echo",
    }))
    _check("self-message filtered", len(dispatched) == 0)

    # 4) DM
    dispatched.clear()
    asyncio.run(a._handle_message_event({
        "id": 103, "type": "direct", "sender_id": 42,
        "sender_full_name": "Tamas",
        "display_recipient": [
            {"id": 42, "email": "tamas@359.wtf"},
            {"id": 11, "email": "ange-bot@example.com"},
        ],
        "content": "private hi",
    }))
    _check("dm dispatched", len(dispatched) == 1)
    _check("dm chat_id sorted users", dispatched[0].source.chat_id == "dm:11,42")
    _check("dm chat_type", dispatched[0].source.chat_type == "dm")
    _check("dm has no thread_id", dispatched[0].source.thread_id is None)

    # 5) Two topics in the same stream produce DIFFERENT chat_id/thread_id combos
    #    (the actual "two sessions" guarantee comes from gateway/session.py,
    #    but we verify the source fields it consumes).
    dispatched.clear()
    asyncio.run(a._handle_message_event({
        "id": 200, "type": "stream", "sender_id": 42, "sender_full_name": "Tamas",
        "display_recipient": "sandbox", "subject": "future-plans",
        "stream_id": 7, "content": "what about Q3?",
    }))
    asyncio.run(a._handle_message_event({
        "id": 201, "type": "stream", "sender_id": 42, "sender_full_name": "Tamas",
        "display_recipient": "sandbox", "subject": "current-bug",
        "stream_id": 7, "content": "the test fails on macOS",
    }))
    _check("two topics dispatched", len(dispatched) == 2)
    _check("topic 1 isolated", dispatched[0].source.thread_id == "future-plans")
    _check("topic 2 isolated", dispatched[1].source.thread_id == "current-bug")
    _check("both share stream chat_id",
           dispatched[0].source.chat_id == dispatched[1].source.chat_id == "stream:sandbox")
    _check("session key differs (chat_id+thread_id)",
           (dispatched[0].source.chat_id, dispatched[0].source.thread_id)
           != (dispatched[1].source.chat_id, dispatched[1].source.thread_id))

    print(f"\nM2 results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    rc1 = run()
    rc2 = run_m2()
    sys.exit(rc1 | rc2)
