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


# --------------------------------------------------------------------------- #
# M4 tests: agent-facing tools
# --------------------------------------------------------------------------- #

def run_m4() -> int:
    import asyncio
    import os
    from unittest.mock import patch, AsyncMock, MagicMock

    print("\nM4: agent-facing tools")

    from hermes_plugins.zulip import tools as zt

    # Without env vars → tools should refuse cleanly
    saved = {k: os.environ.pop(k, None) for k in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY")}
    try:
        r = asyncio.run(zt._handle_zulip_post("s", "t", "hi"))
        _check("post without env → error", r["success"] is False)
        _check("post error mentions env vars", "ZULIP_SITE" in r["error"])
        _check("check_zulip_available() False",
               zt._check_zulip_available() is False)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # With env vars set
    os.environ["ZULIP_SITE"] = "https://example.zulip.com"
    os.environ["ZULIP_EMAIL"] = "ange-bot@example.com"
    os.environ["ZULIP_API_KEY"] = "k"

    _check("check_zulip_available() True", zt._check_zulip_available() is True)

    # zulip_post → mock ZulipClient
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.send_stream_message = AsyncMock(return_value=42)
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_post("sandbox", "auth-bug", "hello"))
    _check("post.success", r["success"] is True)
    _check("post.message_id == 42", r["message_id"] == 42)
    _check("post.url has narrow", "/#narrow/stream/sandbox/topic/auth-bug/near/42" in r["url"])
    fake_client.send_stream_message.assert_awaited_once_with("sandbox", "auth-bug", "hello")
    _check("client called once", True)

    # zulip_post handles API errors gracefully
    fake_client.send_stream_message = AsyncMock(
        side_effect=zt.ZulipAPIError(400, "BAD_REQUEST", "no such stream"),
    )
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_post("nope", "t", "x"))
    _check("post bubbles ZulipAPIError", r["success"] is False)
    _check("post error includes msg", "no such stream" in r["error"])

    # zulip_dm
    fake_client.send_direct_message = AsyncMock(return_value=7)
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_dm(["tamas@359.wtf"], "hi"))
    _check("dm.success", r["success"] is True)
    _check("dm.message_id == 7", r["message_id"] == 7)

    r = asyncio.run(zt._handle_zulip_dm([], "hi"))
    _check("empty recipients rejected", r["success"] is False)

    # zulip_list_streams
    fake_client.get_subscriptions = AsyncMock(return_value=[
        {"name": "sandbox", "stream_id": 7, "description": "Test"},
        {"name": "engineering", "stream_id": 8, "description": "Eng"},
    ])
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_list_streams())
    _check("list_streams count == 2", r["count"] == 2)
    _check("list_streams names", {s["name"] for s in r["streams"]} == {"sandbox", "engineering"})

    # zulip_list_topics
    fake_client._request = AsyncMock(return_value={"topics": [
        {"name": "auth-bug", "max_id": 99},
        {"name": "future-plans", "max_id": 95},
    ]})
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_list_topics("sandbox"))
    _check("list_topics count", r["count"] == 2)
    _check("list_topics ordering preserved",
           [t["name"] for t in r["topics"]] == ["auth-bug", "future-plans"])

    # Unknown stream → not subscribed → error
    fake_client.get_subscriptions = AsyncMock(return_value=[])
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_list_topics("ghost"))
    _check("list_topics rejects unknown stream", r["success"] is False)
    _check("list_topics error mentions stream", "ghost" in r["error"])

    # zulip_upload_image — file-not-found path
    r = asyncio.run(zt._handle_zulip_upload_image("s", "t", "/nope/missing.png"))
    _check("upload_image rejects missing file", r["success"] is False)
    _check("upload_image error mentions path", "/nope/missing.png" in r["error"])

    # Tool registration — verify all 5 tools register cleanly
    registered: list = []

    class _Ctx:
        def register_tool(self, **kw):
            registered.append(kw)

    zt.register_tools(_Ctx())
    _check("registers 5 tools", len(registered) == 5)
    _check("all in hermes-zulip toolset",
           {t["toolset"] for t in registered} == {"hermes-zulip"})
    _check("all async", all(t["is_async"] for t in registered))
    _check("zulip_post registered",
           any(t["name"] == "zulip_post" for t in registered))
    _check("all have requires_env",
           all("ZULIP_SITE" in t["requires_env"] for t in registered))

    # Hint mentions zulip_post (the magic branching tool)
    from hermes_plugins.zulip.adapter import PLATFORM_HINT as HINT
    _check("PLATFORM_HINT names zulip_post", "zulip_post" in HINT)
    _check("PLATFORM_HINT names zulip_list_topics", "zulip_list_topics" in HINT)

    # Regression: every handler must accept task_id (and any future) kwargs
    # injected by the Hermes tool dispatcher. We don't run them — just confirm
    # the signature accepts the extra kwarg without TypeError. (run_m4 in this
    # process has stubbed mode set; the real handlers refuse upfront when
    # ZULIP_SITE is missing.)
    import inspect
    from hermes_plugins.zulip.tools import (
        _handle_zulip_post, _handle_zulip_dm, _handle_zulip_list_streams,
        _handle_zulip_list_topics, _handle_zulip_upload_image,
    )
    for fn in (_handle_zulip_post, _handle_zulip_dm, _handle_zulip_list_streams,
               _handle_zulip_list_topics, _handle_zulip_upload_image):
        sig = inspect.signature(fn)
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        _check(f"{fn.__name__} accepts **kwargs (task_id forward-compat)", has_var_kw)

    print(f"\nM4 results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


# --------------------------------------------------------------------------- #
# M5 tests: inbound media + send_image upload
# --------------------------------------------------------------------------- #

def run_m5() -> int:
    """M5 — attachment parsing, inbound media download, send_image upload."""
    import asyncio
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageType
    from hermes_plugins.zulip.adapter import (
        ZulipAdapter, _parse_user_uploads, _IMAGE_EXTS,
    )

    global _passes, _failures
    _passes = _failures = 0
    print("\nM5: attachment parsing")

    cases = [
        ("plain text no upload", []),
        ("[bug.png](/user_uploads/2/ab/cd/bug.png)",
         [("bug.png", "/user_uploads/2/ab/cd/bug.png")]),
        ("two: [a.png](/user_uploads/2/aa/bb/a.png) and [b.pdf](/user_uploads/2/cc/dd/b.pdf)",
         [("a.png", "/user_uploads/2/aa/bb/a.png"),
          ("b.pdf", "/user_uploads/2/cc/dd/b.pdf")]),
        ("empty name [](/user_uploads/2/x/y/z)",
         [("attachment", "/user_uploads/2/x/y/z")]),
        ("external link [doc](https://example.com/foo.png)", []),
    ]
    for text, want in cases:
        got = _parse_user_uploads(text)
        _check(f"parse {text[:40]!r}", got == want)

    _check("png in _IMAGE_EXTS", ".png" in _IMAGE_EXTS)
    _check("pdf NOT in _IMAGE_EXTS", ".pdf" not in _IMAGE_EXTS)

    # ---- inbound media dispatch with a stubbed client ----
    print("\nM5: inbound media dispatch")
    cfg = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })
    a = ZulipAdapter(cfg)
    a._me = {"user_id": 11}
    a._streams_by_id = {7: {"name": "sandbox", "stream_id": 7, "description": ""}}
    a._streams_by_name = {"sandbox": a._streams_by_id[7]}

    # Stub client with download_user_upload returning a 1x1 PNG.
    PNG_1x1 = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    class _StubClient:
        async def download_user_upload(self, uri):
            return PNG_1x1
    a._client = _StubClient()

    captured = []
    async def _capture(ev):
        captured.append(ev)
    a.handle_message = _capture  # type: ignore[method-assign]

    msg_image = {
        "id": 901, "type": "stream", "sender_id": 8, "sender_full_name": "Tamas",
        "display_recipient": "sandbox", "subject": "screenshots", "stream_id": 7,
        "content": "look at this [screenshot.png](/user_uploads/2/aa/bb/screenshot.png)",
    }
    asyncio.run(a._handle_message_event(msg_image))
    _check("dispatched 1 event", len(captured) == 1)
    if captured:
        ev = captured[0]
        _check("type == PHOTO", ev.message_type == MessageType.PHOTO)
        _check("1 media_url", len(ev.media_urls) == 1)
        _check("media file exists", ev.media_urls and __import__("os").path.exists(ev.media_urls[0]))
        _check("media_type is image/*", ev.media_types and ev.media_types[0].startswith("image/"))

    # Mixed content (image + pdf) stays TEXT type but still carries both files
    captured.clear()
    msg_mixed = dict(msg_image)
    msg_mixed["id"] = 902
    msg_mixed["content"] = "[a.png](/user_uploads/2/x/y/a.png) and [b.pdf](/user_uploads/2/x/y/b.pdf)"
    asyncio.run(a._handle_message_event(msg_mixed))
    if captured:
        ev = captured[0]
        _check("mixed → 2 media", len(ev.media_urls) == 2)
        _check("mixed stays TEXT", ev.message_type == MessageType.TEXT)

    # ---- send_image upload path ----
    print("\nM5: send_image upload")
    sent: dict = {}
    class _StubClient2:
        async def upload_file(self, filename, data, mime):
            sent["upload"] = (filename, len(data), mime)
            return f"/user_uploads/9/zz/{filename}"
        async def send_stream_message(self, stream, topic, content):
            sent["send"] = (stream, topic, content)
            return 12345
    a._client = _StubClient2()

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tf.write(PNG_1x1)
        local_path = tf.name
    res = asyncio.run(a.send_image("stream:sandbox", local_path, caption="hi", thread_id="cats"))
    _check("send_image success", res.success)
    _check("upload was called", "upload" in sent and sent["upload"][1] == len(PNG_1x1))
    _check("upload mime image/png", sent.get("upload", (None,None,None))[2] == "image/png")
    _check("send was called", "send" in sent)
    _check("send target topic", sent.get("send", (None,None,None))[1] == "cats")
    _check("send body has upload uri", "/user_uploads/9/zz/" in (sent.get("send", (None,None,""))[2] or ""))
    _check("send body has caption", "hi" in (sent.get("send", (None,None,""))[2] or ""))

    # Missing local file
    res2 = asyncio.run(a.send_image("stream:sandbox", "/nonexistent/file.png"))
    _check("missing file → failure", not res2.success and "not found" in (res2.error or ""))

    print(f"\nM5 results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    rc1 = run()
    rc2 = run_m2()
    rc3 = run_m4()
    rc4 = run_m5()
    sys.exit(rc1 | rc2 | rc3 | rc4)
