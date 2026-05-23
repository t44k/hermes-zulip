"""Agent-facing Zulip tools.

These are registered via ``ctx.register_tool()`` from ``adapter.py``'s
``register()`` entry point. They give the agent a richer surface than
``send_message`` alone — and crucially, they're how the agent **spawns
new topics** in a stream when the conversation drifts to a new subject.

Tools (all in the ``hermes-zulip`` toolset):

  * ``zulip_post``           — Post to ``stream > topic`` (auto-creates topic)
  * ``zulip_dm``             — Send a direct message to one or more users
  * ``zulip_list_streams``   — List streams the bot is subscribed to
  * ``zulip_list_topics``    — Recent topics inside a stream
  * ``zulip_upload_image``   — Upload a local file + post as inline image

Every tool creates a short-lived ``ZulipClient`` and returns a dict the agent
can read directly. They share the same env vars as the adapter
(``ZULIP_SITE``, ``ZULIP_EMAIL``, ``ZULIP_API_KEY``, ``ZULIP_VERIFY_TLS``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .client import ZulipAPIError, ZulipClient

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _creds() -> tuple[str, str, str, bool] | None:
    site = os.getenv("ZULIP_SITE")
    email = os.getenv("ZULIP_EMAIL")
    api_key = os.getenv("ZULIP_API_KEY")
    verify = os.getenv("ZULIP_VERIFY_TLS", "true").lower() not in {"0", "false", "no", "off"}
    if not (site and email and api_key):
        return None
    return site, email, api_key, verify


def _client() -> ZulipClient | None:
    c = _creds()
    if c is None:
        return None
    site, email, key, verify = c
    return ZulipClient(site, email, key, verify_tls=verify)


def _err(msg: str) -> dict[str, Any]:
    return {"success": False, "error": msg}


def _check_zulip_available() -> bool:
    """``check_fn`` for the toolset — tools hide from the agent without creds."""
    return _creds() is not None


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

ZULIP_POST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_post",
        "description": (
            "Send a message to a Zulip stream topic. Topics are auto-created "
            "on first post — use this to spawn a new topic in an existing "
            "stream when the conversation has drifted to a new subject. "
            "Prefer short kebab-case topic names (≤60 chars). "
            "Returns {success, message_id, url} on success."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Stream name (without leading '#'), e.g. 'sandbox'.",
                },
                "topic": {
                    "type": "string",
                    "description": "Topic name. Kebab-case preferred. New topics are auto-created.",
                },
                "content": {
                    "type": "string",
                    "description": "Message content. Supports Zulip Markdown: **bold**, *italic*, "
                                   "`code`, ```code blocks```, tables, @**mentions**, ||spoilers||.",
                },
            },
            "required": ["stream", "topic", "content"],
        },
    },
}

ZULIP_DM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_dm",
        "description": "Send a direct message to one or more Zulip users by email.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of recipient emails (e.g. ['tamas@359.wtf']). "
                                   "Multiple recipients form a group DM.",
                },
                "content": {"type": "string", "description": "Message content (Zulip Markdown)."},
            },
            "required": ["recipients", "content"],
        },
    },
}

ZULIP_LIST_STREAMS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_list_streams",
        "description": "List streams the bot is subscribed to. Returns name + description for each.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ZULIP_LIST_TOPICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_list_topics",
        "description": "List recent topics inside a stream (newest first). "
                       "Use this to see what threads are active before deciding "
                       "whether to post in an existing topic or spawn a new one.",
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {"type": "string", "description": "Stream name."},
                "limit": {
                    "type": "integer",
                    "description": "Max topics to return (default 25, max 100).",
                    "default": 25,
                },
            },
            "required": ["stream"],
        },
    },
}

ZULIP_UPLOAD_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_upload_image",
        "description": "Upload a local image file to Zulip and post it in a stream topic. "
                       "Returns {success, message_id, file_url}.",
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {"type": "string", "description": "Stream name."},
                "topic": {"type": "string", "description": "Topic name."},
                "path": {"type": "string", "description": "Absolute path to the local image file."},
                "caption": {
                    "type": "string",
                    "description": "Optional Markdown caption posted above the image.",
                    "default": "",
                },
            },
            "required": ["stream", "topic", "path"],
        },
    },
}


# --------------------------------------------------------------------------- #
# Handlers (all async — registered with is_async=True)
# --------------------------------------------------------------------------- #

async def _handle_zulip_post(stream: str, topic: str, content: str) -> dict[str, Any]:
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            mid = await c.send_stream_message(stream, topic, content)
        except ZulipAPIError as e:
            return _err(str(e))
    # Build a deep-link the agent can echo to the user.
    site = os.getenv("ZULIP_SITE", "").rstrip("/")
    # Zulip narrow URL format
    from urllib.parse import quote
    url = (
        f"{site}/#narrow/stream/{quote(stream, safe='')}"
        f"/topic/{quote(topic, safe='')}/near/{mid}"
    )
    return {"success": True, "message_id": mid, "url": url}


async def _handle_zulip_dm(recipients: list[str], content: str) -> dict[str, Any]:
    if not recipients:
        return _err("recipients must be a non-empty list of emails")
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            mid = await c.send_direct_message(recipients, content)
        except ZulipAPIError as e:
            return _err(str(e))
    return {"success": True, "message_id": mid}


async def _handle_zulip_list_streams() -> dict[str, Any]:
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            subs = await c.get_subscriptions()
        except ZulipAPIError as e:
            return _err(str(e))
    out = [
        {
            "name": s.get("name"),
            "stream_id": s.get("stream_id"),
            "description": s.get("description", ""),
        }
        for s in subs
    ]
    return {"success": True, "streams": out, "count": len(out)}


async def _handle_zulip_list_topics(stream: str, limit: int = 25) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        limit = max(1, min(limit, 100))
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            subs = await c.get_subscriptions()
            sid = next(
                (s["stream_id"] for s in subs if s.get("name") == stream), None,
            )
            if sid is None:
                return _err(f"bot is not subscribed to stream '{stream}'")
            resp = await c._request(  # noqa: SLF001
                "GET", f"/users/me/{sid}/topics",
            )
        except ZulipAPIError as e:
            return _err(str(e))
    topics = resp.get("topics", [])[:limit]
    # Zulip returns newest first already; surface name + max_id for context.
    out = [{"name": t.get("name"), "max_id": t.get("max_id")} for t in topics]
    return {"success": True, "stream": stream, "topics": out, "count": len(out)}


async def _handle_zulip_upload_image(
    stream: str, topic: str, path: str, caption: str = "",
) -> dict[str, Any]:
    if not os.path.isfile(path):
        return _err(f"file not found: {path}")
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        # Upload via multipart
        import mimetypes
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        filename = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                resp = await c._request(  # noqa: SLF001
                    "POST",
                    "/user_uploads",
                    files={"file": (filename, f.read(), mime)},
                )
        except ZulipAPIError as e:
            return _err(f"upload failed: {e}")
        file_uri = resp.get("uri") or resp.get("url")
        if not file_uri:
            return _err("upload returned no uri")
        body = f"{caption}\n\n[{filename}]({file_uri})" if caption else f"[{filename}]({file_uri})"
        try:
            mid = await c.send_stream_message(stream, topic, body)
        except ZulipAPIError as e:
            return _err(f"send failed: {e}")
    return {"success": True, "message_id": mid, "file_url": file_uri}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

_TOOLS = (
    ("zulip_post",         ZULIP_POST_SCHEMA,         _handle_zulip_post,         "💬"),
    ("zulip_dm",           ZULIP_DM_SCHEMA,           _handle_zulip_dm,           "📩"),
    ("zulip_list_streams", ZULIP_LIST_STREAMS_SCHEMA, _handle_zulip_list_streams, "📋"),
    ("zulip_list_topics",  ZULIP_LIST_TOPICS_SCHEMA,  _handle_zulip_list_topics,  "🧵"),
    ("zulip_upload_image", ZULIP_UPLOAD_IMAGE_SCHEMA, _handle_zulip_upload_image, "🖼️"),
)


def register_tools(ctx) -> None:
    """Register all Zulip tools. Called from adapter.register()."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="hermes-zulip",
            schema=schema,
            handler=handler,
            check_fn=_check_zulip_available,
            requires_env=["ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY"],
            is_async=True,
            description=schema["function"]["description"],
            emoji=emoji,
        )
