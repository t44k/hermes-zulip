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
    _check("stream:sandbox", _parse_chat_id("stream:sandbox") == ("stream", "sandbox", None))
    _check("stream:sandbox:topic (cron 3-segment)",
           _parse_chat_id("stream:sandbox:m7-cron-test") == ("stream", "sandbox", "m7-cron-test"))
    _check("dm:a@x,b@y",
           _parse_chat_id("dm:a@x.com,b@y.com") == ("dm", "a@x.com,b@y.com", None))
    _check("bare → stream", _parse_chat_id("sandbox") == ("stream", "sandbox", None))
    _check("unknown prefix → stream-whole",
           _parse_chat_id("weird:thing") == ("stream", "weird:thing", None))

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
    _check("ev1.text is prefixed with msg id", ev1.text.startswith("[msg #100] "))
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
        r = asyncio.run(zt._handle_zulip_post({"stream": "s", "topic": "t", "content": "hi"}))
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
        r = asyncio.run(zt._handle_zulip_post({"stream": "sandbox", "topic": "auth-bug", "content": "hello"}))
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
        r = asyncio.run(zt._handle_zulip_post({"stream": "nope", "topic": "t", "content": "x"}))
    _check("post bubbles ZulipAPIError", r["success"] is False)
    _check("post error includes msg", "no such stream" in r["error"])

    # zulip_dm
    fake_client.send_direct_message = AsyncMock(return_value=7)
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_dm({"recipients": ["tamas@359.wtf"], "content": "hi"}))
    _check("dm.success", r["success"] is True)
    _check("dm.message_id == 7", r["message_id"] == 7)

    r = asyncio.run(zt._handle_zulip_dm({"recipients": [], "content": "hi"}))
    _check("empty recipients rejected", r["success"] is False)

    # zulip_list_streams
    fake_client.get_subscriptions = AsyncMock(return_value=[
        {"name": "sandbox", "stream_id": 7, "description": "Test"},
        {"name": "engineering", "stream_id": 8, "description": "Eng"},
    ])
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_list_streams({}))
    _check("list_streams count == 2", r["count"] == 2)
    _check("list_streams names", {s["name"] for s in r["streams"]} == {"sandbox", "engineering"})

    # zulip_list_topics
    fake_client._request = AsyncMock(return_value={"topics": [
        {"name": "auth-bug", "max_id": 99},
        {"name": "future-plans", "max_id": 95},
    ]})
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_list_topics({"stream": "sandbox"}))
    _check("list_topics count", r["count"] == 2)
    _check("list_topics ordering preserved",
           [t["name"] for t in r["topics"]] == ["auth-bug", "future-plans"])

    # Unknown stream → not subscribed → error
    fake_client.get_subscriptions = AsyncMock(return_value=[])
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_list_topics({"stream": "ghost"}))
    _check("list_topics rejects unknown stream", r["success"] is False)
    _check("list_topics error mentions stream", "ghost" in r["error"])

    # zulip_upload_image — file-not-found path
    r = asyncio.run(zt._handle_zulip_upload_image({"stream": "s", "topic": "t", "path": "/nope/missing.png"}))
    _check("upload_image rejects missing file", r["success"] is False)
    _check("upload_image error mentions path", "/nope/missing.png" in r["error"])

    # Tool registration — verify all 5 tools register cleanly
    registered: list = []

    class _Ctx:
        def register_tool(self, **kw):
            registered.append(kw)

    zt.register_tools(_Ctx())
    _check("registers ≥6 tools", len(registered) >= 6)
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

    # Regression: every handler must follow the (args: dict, **kwargs) calling
    # convention — the Hermes tool dispatcher invokes them as
    # ``entry.handler(args, **kwargs)`` where ``args`` is the parsed JSON args
    # dict and kwargs include execution-context keys (task_id, …). Handlers
    # that took unpacked positional args (stream, topic, path, …) crashed live
    # in #sandbox; this test pins the calling convention.
    import inspect
    from hermes_plugins.zulip.tools import (
        _handle_zulip_post, _handle_zulip_dm, _handle_zulip_list_streams,
        _handle_zulip_list_topics, _handle_zulip_upload_image,
    )
    for fn in (_handle_zulip_post, _handle_zulip_dm, _handle_zulip_list_streams,
               _handle_zulip_list_topics, _handle_zulip_upload_image):
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        _check(f"{fn.__name__} accepts **kwargs (task_id forward-compat)", has_var_kw)
        # First positional must accept the args dict (named 'args' by
        # convention; default OK for list_streams which has no required args).
        positional = [p for p in params if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )]
        _check(f"{fn.__name__} takes a single positional args dict",
               len(positional) == 1 and positional[0].name == "args")
        # Smoke: calling with a positional args dict + task_id kwarg must not TypeError.
        try:
            import asyncio as _aio
            _aio.run(fn({}, task_id="probe"))
            crashed = False
        except TypeError as e:
            crashed = True
            print(f"   call-shape TypeError on {fn.__name__}: {e}")
        except Exception:
            # Any *runtime* error (e.g. missing env) is fine — we only fail on TypeError.
            crashed = False
        _check(f"{fn.__name__}(args={{}}, task_id=…) does not TypeError", not crashed)

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


# --------------------------------------------------------------------------- #
# M6 tests: reactions (in + out)
# --------------------------------------------------------------------------- #

def run_m6() -> int:
    """M6 — inbound reaction dispatch + zulip_react tool."""
    import asyncio
    import os as _os
    from unittest.mock import AsyncMock, patch
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageType
    from hermes_plugins.zulip.adapter import ZulipAdapter
    from hermes_plugins.zulip import tools as zt

    global _passes, _failures
    _passes = _failures = 0
    print("\nM6: inbound reaction dispatch")

    cfg = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })
    a = ZulipAdapter(cfg)
    a._me = {"user_id": 11, "email": "ange-bot@example.com", "full_name": "Ange"}
    a._streams_by_id = {7: {"name": "sandbox", "stream_id": 7}}
    a._streams_by_name = {"sandbox": a._streams_by_id[7]}

    captured: list = []
    async def _capture(ev):
        captured.append(ev)
    a.handle_message = _capture  # type: ignore[method-assign]

    # Stub client.get_message — returns a bot-authored stream message
    bot_msg = {
        "id": 555, "sender_id": 11, "type": "stream",
        "display_recipient": "sandbox", "subject": "auth-bug",
    }
    user_msg = dict(bot_msg, id=556, sender_id=8)  # user-authored — out of scope

    class _StubClient:
        target = bot_msg
        async def get_message(self, mid):
            return type(self).target

    a._client = _StubClient()

    # 1) user reacts to bot's message — should dispatch
    ev_add = {
        "type": "reaction", "op": "add",
        "user_id": 8, "user": {"full_name": "Tamas", "user_id": 8},
        "message_id": 555, "emoji_name": "thumbs_up",
    }
    asyncio.run(a._handle_reaction_event(ev_add))
    _check("user→bot reaction dispatched", len(captured) == 1)
    if captured:
        ev = captured[0]
        _check("text contains user", "Tamas" in ev.text)
        _check("text contains emoji", "thumbs_up" in ev.text)
        _check("text mentions reaction", "reacted" in ev.text.lower())
        _check("message_type TEXT", ev.message_type == MessageType.TEXT)
        _check("routed to right stream",
               ev.source.chat_id == "stream:sandbox")
        _check("routed to right topic",
               ev.source.thread_id == "auth-bug")
        _check("user_name = Tamas",
               ev.source.user_name == "Tamas")

    # 2) bot's own reaction echo — should NOT dispatch
    captured.clear()
    ev_self = dict(ev_add, user_id=11, user={"full_name": "Ange", "user_id": 11})
    asyncio.run(a._handle_reaction_event(ev_self))
    _check("self-reaction filtered", len(captured) == 0)

    # 3) "remove" op — should NOT dispatch
    captured.clear()
    asyncio.run(a._handle_reaction_event(dict(ev_add, op="remove")))
    _check("remove op filtered", len(captured) == 0)

    # 4) reaction on user-authored message — should NOT dispatch
    captured.clear()
    _StubClient.target = user_msg
    asyncio.run(a._handle_reaction_event(dict(ev_add, message_id=556)))
    _check("reaction on non-bot msg filtered", len(captured) == 0)

    # ---- zulip_react tool ----
    print("\nM6: zulip_react tool")
    saved = {k: _os.environ.pop(k, None) for k in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY")}
    try:
        r = asyncio.run(zt._handle_zulip_react({"message_id": 5, "emoji_name": "tada"}))
        _check("react without env → error", r["success"] is False)
        _check("react error mentions env", "ZULIP_SITE" in r["error"])
    finally:
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v

    # With env + mocked client
    _os.environ["ZULIP_SITE"] = "https://zulip.example.com"
    _os.environ["ZULIP_EMAIL"] = "ange-bot@example.com"
    _os.environ["ZULIP_API_KEY"] = "k"
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.add_reaction = AsyncMock(return_value=None)
    fake_client.remove_reaction = AsyncMock(return_value=None)

    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_react({"message_id": 42, "emoji_name": "thumbs_up"}))
    _check("react.success default add", r["success"] is True and r["op"] == "add")
    fake_client.add_reaction.assert_awaited_with(42, "thumbs_up")

    # Strip surrounding colons in emoji_name
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_react({"message_id": 42, "emoji_name": ":tada:"}))
    _check("react strips colons from emoji_name", r["emoji_name"] == "tada")

    # remove
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_react({"message_id": 99, "emoji_name": "eyes", "op": "remove"}))
    _check("react remove dispatched", r["success"] is True and r["op"] == "remove")
    fake_client.remove_reaction.assert_awaited_with(99, "eyes")

    # Missing args
    r = asyncio.run(zt._handle_zulip_react({"emoji_name": "tada"}))
    _check("missing message_id → error", not r["success"])
    r = asyncio.run(zt._handle_zulip_react({"message_id": 1, "emoji_name": ""}))
    _check("missing emoji_name → error", not r["success"])
    r = asyncio.run(zt._handle_zulip_react({"message_id": 1, "emoji_name": "x", "op": "bogus"}))
    _check("invalid op → error", not r["success"])

    # Registry — react tool must be present in _TOOLS
    names = [t[0] for t in zt._TOOLS]
    _check("zulip_react registered", "zulip_react" in names)

    print(f"\nM6 results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


# --------------------------------------------------------------------------- #
# M6 polish: "seen / done" eye-reaction lifecycle
# --------------------------------------------------------------------------- #

def run_m6_polish() -> int:
    """Auto eye-reaction on inbound + removal when the turn task completes."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.config import Platform
    from hermes_plugins.zulip.adapter import ZulipAdapter

    global _passes, _failures
    _passes = _failures = 0
    print("\nM6 polish: eye-reaction lifecycle")

    def _mk_adapter() -> ZulipAdapter:
        cfg = PlatformConfig(enabled=True, extra={
            "site": "https://zulip.example.com",
            "email": "ange-bot@example.com",
            "api_key": "k",
        })
        a = ZulipAdapter(cfg)
        a._me = {"user_id": 11}
        a._streams_by_id = {7: {"name": "sandbox", "stream_id": 7}}
        a._streams_by_name = {"sandbox": a._streams_by_id[7]}
        a._client = AsyncMock()
        a._client.add_reaction = AsyncMock(return_value=None)
        a._client.remove_reaction = AsyncMock(return_value=None)
        return a

    def _mk_event(mid="555", text="hello", source_msg_id="555"):
        src = SessionSource(
            platform=Platform("zulip"),
            chat_id="stream:sandbox",
            chat_name="#sandbox",
            chat_type="channel",
            user_id="8",
            user_name="Tamas",
            thread_id="auth-bug",
            parent_chat_id="stream:sandbox",
            message_id=source_msg_id,
        )
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=src, message_id=mid)

    # 1) Successful turn — eye added then removed
    async def _scenario_success():
        a = _mk_adapter()
        completed = asyncio.Event()
        async def _fake_turn():
            await asyncio.sleep(0.01)
        ev = _mk_event()

        # Stub super().handle_message to register a task on _session_tasks (the
        # base class spawns its own background task; we mimic just enough for
        # the wrapper to find one).
        captured_task: list[asyncio.Task] = []
        async def _stub_super(self, event):
            t = asyncio.create_task(_fake_turn())
            self._session_tasks["k"] = t
            captured_task.append(t)
        # Patch super class method
        from gateway.platforms.base import BasePlatformAdapter
        orig = BasePlatformAdapter.handle_message
        BasePlatformAdapter.handle_message = _stub_super  # type: ignore[assignment]
        try:
            await a.handle_message(ev)
            assert captured_task, "stub did not spawn a task"
            await captured_task[0]
            # Allow done callback to schedule removal + run
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            BasePlatformAdapter.handle_message = orig  # type: ignore[assignment]
        return a

    a1 = asyncio.run(_scenario_success())
    _check("success: add_reaction called once", a1._client.add_reaction.await_count == 1)
    _check("success: add_reaction(:eyes:, mid=555)",
           a1._client.add_reaction.call_args.args == (555, "eyes"))
    _check("success: remove_reaction called once",
           a1._client.remove_reaction.await_count == 1)
    _check("success: remove_reaction(:eyes:, mid=555)",
           a1._client.remove_reaction.call_args.args == (555, "eyes"))

    # 2) Turn raises — eye stays in place
    async def _scenario_failure():
        a = _mk_adapter()
        async def _crashy_turn():
            await asyncio.sleep(0.01)
            raise RuntimeError("agent crashed")
        captured_task: list[asyncio.Task] = []
        async def _stub_super(self, event):
            t = asyncio.create_task(_crashy_turn())
            self._session_tasks["k"] = t
            captured_task.append(t)
        from gateway.platforms.base import BasePlatformAdapter
        orig = BasePlatformAdapter.handle_message
        BasePlatformAdapter.handle_message = _stub_super  # type: ignore[assignment]
        try:
            await a.handle_message(_mk_event())
            # Allow the turn to crash + done callback to fire
            try:
                await captured_task[0]
            except RuntimeError:
                pass
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            BasePlatformAdapter.handle_message = orig  # type: ignore[assignment]
        return a

    a2 = asyncio.run(_scenario_failure())
    _check("failure: add_reaction called", a2._client.add_reaction.await_count == 1)
    _check("failure: remove_reaction NOT called",
           a2._client.remove_reaction.await_count == 0)

    # 3) Reaction synthetic event (non-numeric msg_id) — no add at all
    async def _scenario_synthetic():
        a = _mk_adapter()
        async def _stub_super(self, event): pass
        from gateway.platforms.base import BasePlatformAdapter
        orig = BasePlatformAdapter.handle_message
        BasePlatformAdapter.handle_message = _stub_super  # type: ignore[assignment]
        try:
            ev = _mk_event(mid="reaction:555:thumbs_up", source_msg_id="reaction:555:thumbs_up")
            await a.handle_message(ev)
        finally:
            BasePlatformAdapter.handle_message = orig  # type: ignore[assignment]
        return a

    a3 = asyncio.run(_scenario_synthetic())
    _check("synthetic reaction event: no add_reaction",
           a3._client.add_reaction.await_count == 0)

    # 4) auto_seen_reaction=False — disabled, no add
    async def _scenario_disabled():
        a = _mk_adapter()
        a.auto_seen_reaction = False
        async def _stub_super(self, event): pass
        from gateway.platforms.base import BasePlatformAdapter
        orig = BasePlatformAdapter.handle_message
        BasePlatformAdapter.handle_message = _stub_super  # type: ignore[assignment]
        try:
            await a.handle_message(_mk_event())
        finally:
            BasePlatformAdapter.handle_message = orig  # type: ignore[assignment]
        return a

    a4 = asyncio.run(_scenario_disabled())
    _check("disabled: no add_reaction", a4._client.add_reaction.await_count == 0)

    # 5) Custom seen_emoji via config
    cfg5 = PlatformConfig(enabled=True, extra={
        "site": "s", "email": "e", "api_key": "k", "seen_emoji": ":sparkles:",
    })
    a5 = ZulipAdapter(cfg5)
    _check("custom seen_emoji strips colons", a5.seen_emoji == "sparkles")

    print(f"\nM6 polish results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


# --------------------------------------------------------------------------- #
# M8 tests: message edit + delete (in + out)
# --------------------------------------------------------------------------- #

def run_m8() -> int:
    """M8 — update_message event dispatch + zulip_edit / zulip_delete tools."""
    import asyncio
    import os as _os
    from unittest.mock import AsyncMock, patch
    from gateway.config import PlatformConfig
    from gateway.platforms.base import MessageType
    from hermes_plugins.zulip.adapter import ZulipAdapter
    from hermes_plugins.zulip import tools as zt

    global _passes, _failures
    _passes = _failures = 0
    print("\nM8: inbound update_message dispatch")

    cfg = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })
    a = ZulipAdapter(cfg)
    a._me = {"user_id": 11, "email": "ange-bot@example.com", "full_name": "Ange"}
    a._streams_by_id = {7: {"name": "sandbox", "stream_id": 7}}
    a._streams_by_name = {"sandbox": a._streams_by_id[7]}

    # Don't trigger the auto-seen-reaction wrap during dispatch (we're testing
    # routing, not the eye lifecycle); cleanest path is to stub handle_message.
    captured: list = []
    async def _capture(ev):
        captured.append(ev)
    a.handle_message = _capture  # type: ignore[method-assign]

    user_msg = {
        "id": 777, "sender_id": 8, "sender_full_name": "Tamas", "type": "stream",
        "display_recipient": "sandbox", "subject": "auth-bug",
    }
    bot_msg = dict(user_msg, id=778, sender_id=11, sender_full_name="Ange")

    class _StubClient:
        target = user_msg
        async def get_message(self, mid):
            return type(self).target

    a._client = _StubClient()

    # 1) User edits their own message — dispatch synthetic event
    ev_edit = {
        "type": "update_message",
        "message_id": 777,
        "user_id": 8,
        "content": "actually the bug is in auth.py line 42, not 41",
        "orig_content": "the bug is in auth.py line 41",
        "subject": "auth-bug",
    }
    asyncio.run(a._handle_update_message_event(ev_edit))
    _check("user edit dispatched", len(captured) == 1)
    if captured:
        ev = captured[0]
        _check("edit text mentions msg id", "msg #777" in ev.text)
        _check("edit text contains new content",
               "line 42" in ev.text)
        _check("edit text labelled 'edited'", "edited" in ev.text.lower())
        _check("edit routed to stream", ev.source.chat_id == "stream:sandbox")
        _check("edit routed to topic", ev.source.thread_id == "auth-bug")
        _check("edit message_id namespaced",
               ev.source.message_id == "edit:777")
        _check("edit user name = Tamas",
               ev.source.user_name == "Tamas")

    # 2) Bot's own edit echo — should NOT dispatch
    captured.clear()
    ev_self_edit = dict(ev_edit, user_id=11, message_id=778)
    _StubClient.target = bot_msg
    asyncio.run(a._handle_update_message_event(ev_self_edit))
    _check("self-edit filtered", len(captured) == 0)

    # 3) Topic-only rename (no content key) — should NOT dispatch
    captured.clear()
    _StubClient.target = user_msg
    ev_topic_only = {
        "type": "update_message", "message_id": 777, "user_id": 8,
        "orig_subject": "old-name", "subject": "new-name",
    }
    asyncio.run(a._handle_update_message_event(ev_topic_only))
    _check("topic-only edit filtered", len(captured) == 0)

    # 4) Very long edit content gets truncated
    captured.clear()
    long_content = "x" * 5000
    asyncio.run(a._handle_update_message_event(
        dict(ev_edit, content=long_content)
    ))
    _check("long edit dispatched", len(captured) == 1)
    if captured:
        _check("long edit truncated under 2500 chars",
               len(captured[0].text) < 2500)
        _check("long edit ends with ellipsis",
               captured[0].text.endswith("…]"))

    # ---- zulip_edit tool ----
    print("\nM8: zulip_edit tool")
    saved = {k: _os.environ.pop(k, None) for k in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY")}
    try:
        r = asyncio.run(zt._handle_zulip_edit({"message_id": 5, "content": "x"}))
        _check("edit without env → error", r["success"] is False)
        _check("edit env error mentions ZULIP_SITE", "ZULIP_SITE" in r["error"])
    finally:
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v

    _os.environ["ZULIP_SITE"] = "https://zulip.example.com"
    _os.environ["ZULIP_EMAIL"] = "ange-bot@example.com"
    _os.environ["ZULIP_API_KEY"] = "k"

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.update_message = AsyncMock(return_value={"result": "success"})
    fake_client.delete_message = AsyncMock(return_value={"result": "success"})

    # content-only edit
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_edit(
            {"message_id": 42, "content": "fixed text"}
        ))
    _check("edit content-only success", r["success"] is True)
    fake_client.update_message.assert_awaited_with(
        42, content="fixed text", topic=None, propagate_mode=None,
    )

    # topic-only edit, propagate_mode forwarded
    fake_client.update_message.reset_mock()
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_edit({
            "message_id": 43, "topic": "renamed", "propagate_mode": "change_all",
        }))
    _check("edit topic-only success", r["success"] is True)
    fake_client.update_message.assert_awaited_with(
        43, content=None, topic="renamed", propagate_mode="change_all",
    )

    # neither content nor topic → error
    r = asyncio.run(zt._handle_zulip_edit({"message_id": 1}))
    _check("edit with nothing to change → error", not r["success"])

    # invalid propagate_mode → error
    r = asyncio.run(zt._handle_zulip_edit({
        "message_id": 1, "topic": "x", "propagate_mode": "bogus",
    }))
    _check("edit invalid propagate_mode → error", not r["success"])

    # missing message_id
    r = asyncio.run(zt._handle_zulip_edit({"content": "x"}))
    _check("edit missing message_id → error", not r["success"])

    # ---- zulip_delete tool ----
    print("\nM8: zulip_delete tool")
    fake_client.delete_message.reset_mock()
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_delete({"message_id": 99}))
    _check("delete success", r["success"] is True and r["message_id"] == 99)
    fake_client.delete_message.assert_awaited_with(99)

    r = asyncio.run(zt._handle_zulip_delete({}))
    _check("delete missing message_id → error", not r["success"])

    # ---- client.update_message guard ----
    print("\nM8: client.update_message guards")
    from hermes_plugins.zulip.client import ZulipClient
    c_unused = ZulipClient("https://x", "e", "k")
    try:
        asyncio.run(c_unused.update_message(1))
    except ValueError as e:
        _check("client.update_message rejects empty edit",
               "nothing to change" in str(e))
    else:
        _check("client.update_message rejects empty edit", False,
               "expected ValueError")

    # ---- Registry ----
    names = [t[0] for t in zt._TOOLS]
    _check("zulip_edit registered", "zulip_edit" in names)
    _check("zulip_delete registered", "zulip_delete" in names)
    _check("tool count >= 8", len(names) >= 8)

    print(f"\nM8 results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


# --------------------------------------------------------------------------- #
# M9 tests: outgoing ID tagging + zulip_fetch
# --------------------------------------------------------------------------- #

def run_m9() -> int:
    """M9 — auto-tag outbound [msg #N] + zulip_fetch history tool."""
    import asyncio
    import os as _os
    from unittest.mock import AsyncMock, patch
    from gateway.config import PlatformConfig
    from hermes_plugins.zulip.adapter import ZulipAdapter
    from hermes_plugins.zulip import tools as zt

    global _passes, _failures
    _passes = _failures = 0

    # ---- outbound ID tagging ----
    print("\nM9: outbound [msg #N] auto-tag")
    cfg = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })
    a = ZulipAdapter(cfg)
    a._me = {"user_id": 11, "email": "ange-bot@example.com"}
    a._client = AsyncMock()
    a._client.send_stream_message = AsyncMock(return_value=12345)
    a._client.send_direct_message = AsyncMock(return_value=12346)
    a._client.update_message = AsyncMock(return_value={"result": "success"})

    # 1) Stream send tags
    r = asyncio.run(a.send("stream:sandbox", "hello world", thread_id="t"))
    _check("stream send success", r.success and r.message_id == "12345")
    a._client.update_message.assert_awaited_with(
        12345, content="[msg #12345] hello world",
    )

    # 2) DM send tags
    a._client.update_message.reset_mock()
    r = asyncio.run(a.send("dm:tamas@359.wtf", "ping"))
    _check("dm send success", r.success and r.message_id == "12346")
    a._client.update_message.assert_awaited_with(
        12346, content="[msg #12346] ping",
    )

    # 3) Already-tagged body → skip re-tag
    a._client.update_message.reset_mock()
    asyncio.run(a.send("stream:sandbox", "[msg #99] already tagged", thread_id="t"))
    _check("pre-tagged body skips re-tag",
           a._client.update_message.await_count == 0)

    # 4) Disabled flag → no PATCH
    a.tag_outgoing_ids = False
    a._client.update_message.reset_mock()
    asyncio.run(a.send("stream:sandbox", "no tag please", thread_id="t"))
    _check("disabled flag → no update_message",
           a._client.update_message.await_count == 0)
    a.tag_outgoing_ids = True

    # 5) PATCH failure is non-fatal — send still returns success
    a._client.update_message.reset_mock()
    a._client.update_message.side_effect = RuntimeError("boom")
    r = asyncio.run(a.send("stream:sandbox", "still works", thread_id="t"))
    _check("PATCH failure swallowed; send succeeds",
           r.success and r.message_id == "12345")
    a._client.update_message.side_effect = None

    # 6) Regression: cron-style 3-segment chat_id `stream:foo:topic`
    #    must route to stream=foo, topic=topic (not stream='foo:topic').
    a._client.send_stream_message.reset_mock()
    a._client.update_message.reset_mock()
    r = asyncio.run(a.send("stream:sandbox:m7-cron-test", "from cron"))
    _check("3-segment chat_id success", r.success)
    a._client.send_stream_message.assert_awaited_with(
        "sandbox", "m7-cron-test", "from cron",
    )

    # 7) Explicit thread_id wins over embedded chat_id topic
    a._client.send_stream_message.reset_mock()
    r = asyncio.run(a.send(
        "stream:sandbox:legacy-topic", "msg", thread_id="explicit-topic",
    ))
    _check("explicit thread_id beats embedded", r.success)
    a._client.send_stream_message.assert_awaited_with(
        "sandbox", "explicit-topic", "msg",
    )

    # ---- zulip_fetch tool ----
    print("\nM9: zulip_fetch tool")
    saved = {k: _os.environ.pop(k, None) for k in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY")}
    try:
        r = asyncio.run(zt._handle_zulip_fetch({"stream": "sandbox"}))
        _check("fetch without env → error", not r["success"])
    finally:
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v

    _os.environ["ZULIP_SITE"] = "https://zulip.example.com"
    _os.environ["ZULIP_EMAIL"] = "ange-bot@example.com"
    _os.environ["ZULIP_API_KEY"] = "k"

    fake_msgs = {
        "messages": [
            {
                "id": 100, "sender_id": 8, "sender_full_name": "Tamas",
                "timestamp": 1700000000, "type": "stream",
                "display_recipient": "sandbox", "subject": "auth-bug",
                "content": "the bug is in auth.py",
            },
            {
                "id": 101, "sender_id": 11, "sender_full_name": "Ange",
                "timestamp": 1700000010, "type": "stream",
                "display_recipient": "sandbox", "subject": "auth-bug",
                "content": "[msg #101] looking into it",
            },
        ],
        "found_oldest": False,
        "found_newest": True,
        "anchor": 101,
    }
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get_messages = AsyncMock(return_value=fake_msgs)

    # 1) stream + topic narrow
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        r = asyncio.run(zt._handle_zulip_fetch({
            "stream": "sandbox", "topic": "auth-bug", "num_before": 5,
        }))
    _check("fetch success", r["success"])
    _check("fetch count == 2", r["count"] == 2)
    _check("fetch compact: id present", r["messages"][0]["id"] == 100)
    _check("fetch compact: sender present", r["messages"][0]["sender"] == "Tamas")
    _check("fetch compact: stream/topic", r["messages"][0]["stream"] == "sandbox" and r["messages"][0]["topic"] == "auth-bug")
    fake_client.get_messages.assert_awaited_with(
        anchor="newest", num_before=5, num_after=0,
        narrow=[
            {"operator": "stream", "operand": "sandbox"},
            {"operator": "topic", "operand": "auth-bug"},
        ],
    )

    # 2) numeric anchor string coerced to int
    fake_client.get_messages.reset_mock()
    with patch.object(zt, "ZulipClient", return_value=fake_client):
        asyncio.run(zt._handle_zulip_fetch({"anchor": "555", "num_before": 3}))
    args = fake_client.get_messages.await_args
    _check("numeric anchor str coerced to int", args.kwargs["anchor"] == 555)

    # 3) topic without stream → error
    r = asyncio.run(zt._handle_zulip_fetch({"topic": "x"}))
    _check("topic w/o stream → error", not r["success"])

    # 4) invalid anchor → error
    r = asyncio.run(zt._handle_zulip_fetch({"anchor": "bogus"}))
    _check("invalid anchor → error", not r["success"])

    # 5) Registry
    names = [t[0] for t in zt._TOOLS]
    _check("zulip_fetch registered", "zulip_fetch" in names)
    _check("tool count == 9", len(names) == 9)

    # 6) client.get_messages anchor validation
    print("\nM9: client.get_messages guards")
    from hermes_plugins.zulip.client import ZulipClient
    c = ZulipClient("https://x", "e", "k")
    try:
        asyncio.run(c.get_messages(anchor="weird"))
    except ValueError:
        _check("client rejects bad anchor", True)
    else:
        _check("client rejects bad anchor", False, "expected ValueError")

    print(f"\nM9 results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


# --------------------------------------------------------------------------- #
# M7 tests: standalone sender (cron + send_message fallback)
# --------------------------------------------------------------------------- #

def run_m7() -> int:
    """M7 — _standalone_send: thread_id, media_files, tagging, chat_id formats."""
    import asyncio
    import tempfile
    from unittest.mock import AsyncMock, patch
    from gateway.config import PlatformConfig
    from hermes_plugins.zulip.adapter import _standalone_send

    global _passes, _failures
    _passes = _failures = 0
    print("\nM7: standalone sender")

    pcfg = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
    })

    def _mk_fake_client(send_id: int = 5001):
        fc = AsyncMock()
        fc.__aenter__ = AsyncMock(return_value=fc)
        fc.__aexit__ = AsyncMock(return_value=None)
        fc.send_stream_message = AsyncMock(return_value=send_id)
        fc.send_direct_message = AsyncMock(return_value=send_id)
        fc.update_message = AsyncMock(return_value={"result": "success"})
        fc.upload_file = AsyncMock(return_value="/user_uploads/2/ab/cd/x.png")
        return fc

    # 1) thread_id kwarg wins over chat_id legacy topic
    fc = _mk_fake_client()
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        r = asyncio.run(_standalone_send(
            pcfg, "stream:sandbox:legacy-topic", "hello",
            thread_id="cron-results",
        ))
    _check("send success", r["success"] and r["message_id"] == "5001")
    fc.send_stream_message.assert_awaited_with("sandbox", "cron-results", "hello")
    fc.update_message.assert_awaited_with(5001, content="[msg #5001] hello")

    # 2) legacy chat_id with topic (no thread_id) still works
    fc = _mk_fake_client(5002)
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        r = asyncio.run(_standalone_send(
            pcfg, "stream:sandbox:topicX", "msg",
        ))
    _check("legacy chat_id topic", r["success"])
    fc.send_stream_message.assert_awaited_with("sandbox", "topicX", "msg")

    # 3) bare 'stream:name' + no thread_id → DEFAULT_TOPIC
    fc = _mk_fake_client(5003)
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        r = asyncio.run(_standalone_send(pcfg, "stream:sandbox", "msg"))
    _check("default topic fallback", r["success"])
    fc.send_stream_message.assert_awaited_with("sandbox", "(no topic)", "msg")

    # 4) DM path
    fc = _mk_fake_client(5004)
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        r = asyncio.run(_standalone_send(
            pcfg, "dm:tamas@359.wtf,other@x.com", "ping",
        ))
    _check("dm send success", r["success"])
    fc.send_direct_message.assert_awaited_with(
        ["tamas@359.wtf", "other@x.com"], "ping",
    )

    # 5) Media upload — file attached to body
    tmpf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmpf.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    tmpf.close()
    fc = _mk_fake_client(5005)
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        r = asyncio.run(_standalone_send(
            pcfg, "stream:sandbox", "caption",
            thread_id="art", media_files=[tmpf.name],
        ))
    _check("media send success", r["success"])
    _check("upload was called", fc.upload_file.await_count == 1)
    sent_body = fc.send_stream_message.await_args.args[2]
    _check("body has caption", "caption" in sent_body)
    _check("body has attachment link", "/user_uploads/" in sent_body)

    # Missing media file → warning logged, still sends text
    fc = _mk_fake_client(5006)
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        r = asyncio.run(_standalone_send(
            pcfg, "stream:sandbox", "no img",
            thread_id="t", media_files=["/nonexistent/file.png"],
        ))
    _check("missing media: still success", r["success"])
    _check("missing media: no upload", fc.upload_file.await_count == 0)

    # 6) Auto-tag respects extra flag
    pcfg_no_tag = PlatformConfig(enabled=True, extra={
        "site": "https://zulip.example.com",
        "email": "ange-bot@example.com",
        "api_key": "k",
        "tag_outgoing_ids": "false",
    })
    fc = _mk_fake_client(5007)
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        asyncio.run(_standalone_send(pcfg_no_tag, "stream:sandbox", "no tag", thread_id="t"))
    _check("tag flag off → no update_message",
           fc.update_message.await_count == 0)

    # 7) Pre-tagged body skips re-tag
    fc = _mk_fake_client(5008)
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        asyncio.run(_standalone_send(
            pcfg, "stream:sandbox", "[msg #99] already tagged", thread_id="t",
        ))
    _check("pre-tagged body skips re-tag",
           fc.update_message.await_count == 0)

    # 8) Missing credentials → error
    pcfg_bare = PlatformConfig(enabled=True, extra={})
    # Need to make sure env vars are not set — _standalone_send falls back to env
    import os as _os
    saved = {k: _os.environ.pop(k, None) for k in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY")}
    try:
        r = asyncio.run(_standalone_send(pcfg_bare, "stream:sandbox", "x"))
        _check("missing creds → error", not r["success"] and "credentials" in r["error"])
    finally:
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v

    # 9) Empty stream → error
    r = asyncio.run(_standalone_send(pcfg, "stream:", "x", thread_id="t"))
    _check("empty stream → error", not r["success"])

    # 10) ZulipAPIError propagates as failure dict
    from hermes_plugins.zulip.client import ZulipAPIError
    fc = _mk_fake_client(5010)
    fc.send_stream_message.side_effect = ZulipAPIError(400, "BAD_REQUEST", "no such stream")
    with patch("hermes_plugins.zulip.adapter.ZulipClient", return_value=fc):
        r = asyncio.run(_standalone_send(pcfg, "stream:nope", "x", thread_id="t"))
    _check("api error → success=False", not r["success"])
    _check("api error message preserved", "no such stream" in r["error"])

    print(f"\nM7 results: {_passes} passed, {_failures} failed")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    rc1 = run()
    rc2 = run_m2()
    rc3 = run_m4()
    rc4 = run_m5()
    rc5 = run_m6()
    rc6 = run_m6_polish()
    rc7 = run_m8()
    rc8 = run_m9()
    rc9 = run_m7()
    sys.exit(rc1 | rc2 | rc3 | rc4 | rc5 | rc6 | rc7 | rc8 | rc9)
