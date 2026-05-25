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


ZULIP_EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_edit",
        "description": (
            "Edit one of your own previous Zulip messages in place. Use this "
            "to (a) fix a typo or factual error without spamming a follow-up, "
            "(b) progressively update a long streamed answer as new tokens "
            "arrive, or (c) move a message to a different topic. At least "
            "one of `content` or `topic` must be provided. Returns {success}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": "ID of the message to edit (use the [msg #N] prefix from the inbound event, or the message_id returned by zulip_post).",
                },
                "content": {
                    "type": "string",
                    "description": "New message body (Zulip Markdown). Omit to leave the body unchanged.",
                },
                "topic": {
                    "type": "string",
                    "description": "New topic name. Omit to keep the current topic. Stream cannot be changed.",
                },
                "propagate_mode": {
                    "type": "string",
                    "enum": ["change_one", "change_later", "change_all"],
                    "description": "When `topic` is set, controls cascade: change_one (just this), change_later (this + later), change_all (every msg in old topic). Default change_one.",
                    "default": "change_one",
                },
            },
            "required": ["message_id"],
        },
    },
}

ZULIP_DELETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_delete",
        "description": (
            "Delete one of your own previous Zulip messages. Useful for "
            "retracting an outdated or wrong reply. The bot can only delete "
            "messages it sent (unless the realm grants broader rights). "
            "Returns {success}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": "ID of the message to delete.",
                },
            },
            "required": ["message_id"],
        },
    },
}


ZULIP_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_fetch",
        "description": (
            "Read recent message history from a Zulip stream/topic or DM. "
            "Use this to summarise a thread, quote earlier messages, look "
            "up the `[msg #N]` id of an old post you want to edit/delete, "
            "or catch up on context before replying. "
            "Provide EITHER (stream + optional topic) OR (anchor) — not "
            "both. Returns {success, messages: [{id, sender, content, "
            "timestamp, stream, topic}], found_oldest, found_newest}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Stream name to narrow to. Omit for cross-stream fetch.",
                },
                "topic": {
                    "type": "string",
                    "description": "Topic within the stream. Requires `stream`. Omit to fetch the whole stream.",
                },
                "anchor": {
                    "type": "string",
                    "description": "Anchor message id (as a string) or one of 'newest'|'oldest'|'first_unread'. Default 'newest'.",
                    "default": "newest",
                },
                "num_before": {
                    "type": "integer",
                    "description": "Number of messages BEFORE the anchor to fetch (default 20, max 1000).",
                    "default": 20,
                },
                "num_after": {
                    "type": "integer",
                    "description": "Number of messages AFTER the anchor to fetch (default 0).",
                    "default": 0,
                },
            },
            "required": [],
        },
    },
}


ZULIP_REACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_react",
        "description": (
            "Add or remove an emoji reaction on a Zulip message. Use this for "
            "lightweight acknowledgements (e.g. 👍 on a request) without "
            "posting a full reply. Emoji name is Zulip's short name like "
            "'thumbs_up', 'tada', 'eyes', 'check'. Returns {success}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": "ID of the message to react to.",
                },
                "emoji_name": {
                    "type": "string",
                    "description": "Zulip emoji short name (no colons), e.g. 'thumbs_up', 'tada', 'eyes', 'sparkles', 'check'.",
                },
                "op": {
                    "type": "string",
                    "enum": ["add", "remove"],
                    "description": "Whether to add or remove the reaction (default: add).",
                    "default": "add",
                },
            },
            "required": ["message_id", "emoji_name"],
        },
    },
}


ZULIP_CREATE_CHANNEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_create_channel",
        "description": (
            "Create a Zulip channel (stream) and/or subscribe users to it. "
            "If the channel already exists, this just adds principals to it. "
            "The bot itself is always subscribed. Pass `principals` to "
            "invite people at creation time (emails or numeric user_ids). "
            "Set `invite_only=true` for a private channel. "
            "Requires stream-creation rights on the bot account. "
            "Returns {success, created, already_subscribed, subscribed, url}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Channel (stream) name. Use plain text, no leading '#'.",
                },
                "description": {
                    "type": "string",
                    "description": "Short description shown in the channel header.",
                },
                "invite_only": {
                    "type": "boolean",
                    "description": "If true, create a private (invite-only) channel. Default false.",
                    "default": False,
                },
                "principals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional users to subscribe alongside the bot. Each item is "
                        "either an email address or a numeric user_id (as a string). "
                        "Use `zulip_list_users` to resolve names → emails."
                    ),
                },
                "announce": {
                    "type": "boolean",
                    "description": (
                        "If true, post an announcement in the realm's notification "
                        "channel when a new public channel is created. Default false."
                    ),
                    "default": False,
                },
                "history_public_to_subscribers": {
                    "type": "boolean",
                    "description": (
                        "For invite_only channels: whether new subscribers can read "
                        "history from before they joined. Defaults to Zulip's realm "
                        "default if omitted."
                    ),
                },
            },
            "required": ["name"],
        },
    },
}


ZULIP_INVITE_TO_CHANNEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_invite_to_channel",
        "description": (
            "Subscribe one or more users to an existing Zulip channel. "
            "Same endpoint as `zulip_create_channel` but intended for "
            "channels that already exist. Principals are emails or "
            "numeric user_ids (as strings). For private channels, the "
            "bot must already be subscribed (or be an admin). "
            "Returns {success, subscribed, already_subscribed}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Channel (stream) name to invite users to.",
                },
                "principals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Users to invite — emails or numeric user_ids (as strings). At least one required.",
                },
            },
            "required": ["stream", "principals"],
        },
    },
}


ZULIP_UNSUBSCRIBE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_unsubscribe",
        "description": (
            "Unsubscribe from a Zulip channel. Without `principals`, the "
            "bot unsubscribes itself. With `principals`, removes those "
            "users from the channel (requires admin or stream-admin role). "
            "This does NOT delete or archive the channel — use "
            "`zulip_archive_channel` for that. Returns {success, removed, not_removed}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Channel (stream) name to unsubscribe from.",
                },
                "principals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional users to unsubscribe instead of the bot itself — "
                        "emails or numeric user_ids (as strings)."
                    ),
                },
            },
            "required": ["stream"],
        },
    },
}


ZULIP_ARCHIVE_CHANNEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_archive_channel",
        "description": (
            "Archive (delete) a Zulip channel. This is admin-only on "
            "most realms — if the bot lacks the role the API returns an "
            "error which is surfaced to you. Archiving is reversible "
            "from the Zulip web UI but not from this tool. "
            "Returns {success, stream_id}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Channel (stream) name to archive.",
                },
            },
            "required": ["stream"],
        },
    },
}


ZULIP_LIST_USERS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_list_users",
        "description": (
            "List users in the Zulip realm. Use this to resolve a person's "
            "name to their email/user_id before inviting them to a channel. "
            "Returns {success, users: [{user_id, email, full_name, is_bot, is_active}], count}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter applied to "
                        "full_name and email. Omit to return everyone."
                    ),
                },
                "include_bots": {
                    "type": "boolean",
                    "description": "Include bot accounts in results. Default false.",
                    "default": False,
                },
                "include_inactive": {
                    "type": "boolean",
                    "description": "Include deactivated users. Default false.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}


ZULIP_BUTTONS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "zulip_buttons",
        "description": (
            "Send a Zulip message with clickable reply buttons (uses Zulip's "
            "built-in `zform` widget). When a user clicks a button, the "
            "configured `reply` string is sent back to the channel as a new "
            "message *from that user* — the agent will receive it as an "
            "ordinary inbound message and can act on it. "
            "**Important caveats:** "
            "(1) Buttons render only in the Zulip web and desktop apps; "
            "mobile + 3rd-party clients see the plain-text fallback. "
            "(2) Choices are radio-style only — no free-text input, no date "
            "pickers, no multi-select. "
            "(3) Prefix every `reply` with a stable slash token "
            "(e.g. `/approval`, `/confirm`, `/cancel`) so the agent can "
            "pattern-match outstanding forms; with trigger_mode=mention_only, "
            "replies starting with a configured prefix bypass the mention gate. "
            "Provide either (stream + topic) OR dm_to. Returns {success, message_id, url?}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stream": {
                    "type": "string",
                    "description": "Stream name (omit when sending as a DM).",
                },
                "topic": {
                    "type": "string",
                    "description": "Topic name (required when stream is set).",
                },
                "dm_to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient emails for a DM. Mutually exclusive with stream.",
                },
                "heading": {
                    "type": "string",
                    "description": "Short question shown above the buttons (also used in the text fallback).",
                },
                "choices": {
                    "type": "array",
                    "description": "Buttons to render, max 10. Each choice needs label + reply.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Visible button text (e.g. 'yes', 'no', 'needs review').",
                            },
                            "reply": {
                                "type": "string",
                                "description": "Message text sent on click. Prefix with a slash token like '/approval v0.3 yes'.",
                            },
                        },
                        "required": ["label", "reply"],
                    },
                    "minItems": 1,
                    "maxItems": 10,
                },
                "fallback_text": {
                    "type": "string",
                    "description": (
                        "Optional text body shown on non-web clients. If omitted, "
                        "an auto-rendering of heading + numbered choices is used."
                    ),
                },
            },
            "required": ["heading", "choices"],
        },
    },
}


# --------------------------------------------------------------------------- #
# Handlers (all async — registered with is_async=True)
# --------------------------------------------------------------------------- #

async def _handle_zulip_post(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = args.get("stream", "")
    topic = args.get("topic", "")
    content = args.get("content", "")
    if not stream or not topic:
        return _err("stream and topic are required")
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


async def _handle_zulip_dm(args: dict, **_kwargs: Any) -> dict[str, Any]:
    recipients = args.get("recipients") or []
    content = args.get("content", "")
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


async def _handle_zulip_list_streams(args: dict | None = None, **_kwargs: Any) -> dict[str, Any]:
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


async def _handle_zulip_list_topics(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = args.get("stream", "")
    limit = int(args.get("limit", 25) or 25)
    if not stream:
        return _err("stream is required")
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


async def _handle_zulip_upload_image(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = args.get("stream", "")
    topic = args.get("topic", "")
    path = args.get("path", "")
    caption = args.get("caption", "") or ""
    if not stream or not topic or not path:
        return _err("stream, topic, and path are required")
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


async def _handle_zulip_react(args: dict, **_kwargs: Any) -> dict[str, Any]:
    msg_id = args.get("message_id")
    emoji_name = (args.get("emoji_name") or "").lstrip(":").rstrip(":").strip()
    op = (args.get("op") or "add").lower()
    if not msg_id:
        return _err("message_id is required")
    if not emoji_name:
        return _err("emoji_name is required (Zulip short name, no colons)")
    if op not in ("add", "remove"):
        return _err("op must be 'add' or 'remove'")
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            if op == "add":
                await c.add_reaction(int(msg_id), emoji_name)
            else:
                await c.remove_reaction(int(msg_id), emoji_name)
        except ZulipAPIError as e:
            return _err(str(e))
    return {"success": True, "message_id": int(msg_id), "emoji_name": emoji_name, "op": op}


async def _handle_zulip_edit(args: dict, **_kwargs: Any) -> dict[str, Any]:
    msg_id = args.get("message_id")
    content = args.get("content")
    topic = args.get("topic")
    propagate = args.get("propagate_mode") or "change_one"
    if not msg_id:
        return _err("message_id is required")
    if content is None and not topic:
        return _err("at least one of `content` or `topic` must be provided")
    if propagate not in ("change_one", "change_later", "change_all"):
        return _err("propagate_mode must be one of change_one|change_later|change_all")
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            await c.update_message(
                int(msg_id),
                content=content,
                topic=topic,
                propagate_mode=propagate if topic else None,
            )
        except ZulipAPIError as e:
            return _err(str(e))
    return {"success": True, "message_id": int(msg_id)}


async def _handle_zulip_delete(args: dict, **_kwargs: Any) -> dict[str, Any]:
    msg_id = args.get("message_id")
    if not msg_id:
        return _err("message_id is required")
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            await c.delete_message(int(msg_id))
        except ZulipAPIError as e:
            return _err(str(e))
    return {"success": True, "message_id": int(msg_id)}


async def _handle_zulip_fetch(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = (args.get("stream") or "").strip()
    topic = (args.get("topic") or "").strip()
    anchor_raw = args.get("anchor", "newest")
    num_before = int(args.get("num_before", 20) or 20)
    num_after = int(args.get("num_after", 0) or 0)

    if topic and not stream:
        return _err("`topic` requires `stream`")

    # Coerce anchor: numeric strings → int, sentinels stay as-is.
    anchor: Any = anchor_raw
    if isinstance(anchor_raw, str) and anchor_raw.isdigit():
        anchor = int(anchor_raw)
    elif isinstance(anchor_raw, str) and anchor_raw not in ("newest", "oldest", "first_unread"):
        return _err("anchor must be a message id (int/numeric str) or 'newest'|'oldest'|'first_unread'")

    narrow: list[dict] = []
    if stream:
        narrow.append({"operator": "stream", "operand": stream})
    if topic:
        narrow.append({"operator": "topic", "operand": topic})

    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            resp = await c.get_messages(
                anchor=anchor,
                num_before=num_before,
                num_after=num_after,
                narrow=narrow or None,
            )
        except (ZulipAPIError, ValueError) as e:
            return _err(str(e))

    # Compact each message — full payloads are huge (rendered_content,
    # avatar urls, reactions, ...). Agent only needs id/sender/content/route.
    out_msgs = []
    for m in resp.get("messages", []):
        out_msgs.append({
            "id": m.get("id"),
            "sender_id": m.get("sender_id"),
            "sender": m.get("sender_full_name") or m.get("sender_email"),
            "timestamp": m.get("timestamp"),
            "type": m.get("type"),
            "stream": m.get("display_recipient") if m.get("type") == "stream" else None,
            "topic": m.get("subject") if m.get("type") == "stream" else None,
            "content": m.get("content", ""),
        })
    return {
        "success": True,
        "count": len(out_msgs),
        "messages": out_msgs,
        "found_oldest": bool(resp.get("found_oldest")),
        "found_newest": bool(resp.get("found_newest")),
        "anchor": resp.get("anchor"),
    }


# --------------------------------------------------------------------------- #
# M13: channel + user management handlers
# --------------------------------------------------------------------------- #

def _principals_typed(raw: Any) -> list[str | int] | None:
    """Normalise a principals list — numeric strings become ints (user_id).

    Accepts ``None``, an actual list, or a JSON-encoded string (some tool
    bridges pass array args through as their JSON text rather than decoding).
    A bare string that doesn't parse as a JSON list is wrapped as ``[raw]`` so
    a single email like ``"alice@example.com"`` still works.
    """
    if raw is None or raw == "" or raw == []:
        return None
    # Some MCP / tool bridges hand us the JSON text of the array, not the
    # decoded list. Try to decode before iterating — otherwise we'd iterate
    # the characters of the JSON string ('[', '"', 'd', '8', ...) which is
    # how the original M13 smoke test produced a 400 from Zulip.
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            import json as _json
            try:
                raw = _json.loads(s)
            except Exception:
                raw = [s]
        else:
            raw = [s]
    if not isinstance(raw, list):
        raise ValueError(f"principals must be a list, got {type(raw).__name__}")
    out: list[str | int] = []
    for item in raw:
        if isinstance(item, int):
            out.append(item)
            continue
        s = str(item).strip()
        if not s:
            continue
        if s.isdigit():
            out.append(int(s))
        else:
            out.append(s)
    return out or None


async def _handle_zulip_create_channel(args: dict, **_kwargs: Any) -> dict[str, Any]:
    name = args.get("name", "").strip()
    if not name:
        return _err("name is required")
    description = args.get("description")
    invite_only = bool(args.get("invite_only", False))
    announce = bool(args.get("announce", False))
    hpts = args.get("history_public_to_subscribers")
    principals = _principals_typed(args.get("principals"))

    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            resp = await c.create_or_subscribe_stream(
                name,
                description=description,
                invite_only=invite_only,
                principals=principals,
                announce=announce,
                history_public_to_subscribers=hpts,
            )
        except ZulipAPIError as e:
            return _err(str(e))

    site = os.getenv("ZULIP_SITE", "").rstrip("/")
    from urllib.parse import quote
    url = f"{site}/#narrow/stream/{quote(name, safe='')}" if site else None
    return {
        "success": True,
        "created": not bool(resp.get("already_subscribed")) or bool(resp.get("subscribed")),
        "subscribed": resp.get("subscribed", {}),
        "already_subscribed": resp.get("already_subscribed", {}),
        "unauthorized": resp.get("unauthorized", []),
        "url": url,
    }


async def _handle_zulip_invite_to_channel(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = args.get("stream", "").strip()
    principals = _principals_typed(args.get("principals"))
    if not stream:
        return _err("stream is required")
    if not principals:
        return _err("principals must be a non-empty list of emails or user_ids")

    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            resp = await c.create_or_subscribe_stream(stream, principals=principals)
        except ZulipAPIError as e:
            return _err(str(e))
    return {
        "success": True,
        "subscribed": resp.get("subscribed", {}),
        "already_subscribed": resp.get("already_subscribed", {}),
        "unauthorized": resp.get("unauthorized", []),
    }


async def _handle_zulip_unsubscribe(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = args.get("stream", "").strip()
    principals = _principals_typed(args.get("principals"))
    if not stream:
        return _err("stream is required")
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            resp = await c.unsubscribe_from_stream([stream], principals=principals)
        except ZulipAPIError as e:
            return _err(str(e))
    return {
        "success": True,
        "removed": resp.get("removed", []),
        "not_removed": resp.get("not_removed", []),
    }


async def _handle_zulip_archive_channel(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = args.get("stream", "").strip()
    if not stream:
        return _err("stream is required")
    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            stream_id = await c.get_stream_id(stream)
            await c.archive_stream(stream_id)
        except ZulipAPIError as e:
            return _err(str(e))
    return {"success": True, "stream_id": stream_id, "stream": stream}


async def _handle_zulip_list_users(args: dict | None = None, **_kwargs: Any) -> dict[str, Any]:
    args = args or {}
    search = (args.get("search") or "").strip().lower()
    include_bots = bool(args.get("include_bots", False))
    include_inactive = bool(args.get("include_inactive", False))

    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            members = await c.get_users()
        except ZulipAPIError as e:
            return _err(str(e))

    out: list[dict[str, Any]] = []
    for m in members:
        if not include_bots and m.get("is_bot"):
            continue
        if not include_inactive and not m.get("is_active", True):
            continue
        full_name = m.get("full_name", "") or ""
        email = m.get("email", "") or m.get("delivery_email", "") or ""
        if search and search not in full_name.lower() and search not in email.lower():
            continue
        out.append({
            "user_id": m.get("user_id"),
            "email": email,
            "full_name": full_name,
            "is_bot": bool(m.get("is_bot")),
            "is_active": bool(m.get("is_active", True)),
        })
    return {"success": True, "users": out, "count": len(out)}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

# --- M14: zform / button widgets ------------------------------------------- #

# Slash-prefix tokens that, when a stream message starts with one of them,
# bypass the trigger_mode=mention_only gate (so button-click replies wake the
# agent without users needing to embed an @-mention in the configured reply).
# Read by adapter._handle_message_event via _zulip_widget_reply_prefixes().
DEFAULT_WIDGET_REPLY_PREFIXES = ("/approval", "/confirm", "/cancel", "/zform")


def _widget_reply_prefixes() -> tuple[str, ...]:
    """Effective allow-list. Overridable by env ``ZULIP_WIDGET_REPLY_PREFIXES``
    (comma-separated)."""
    raw = os.getenv("ZULIP_WIDGET_REPLY_PREFIXES")
    if not raw:
        return DEFAULT_WIDGET_REPLY_PREFIXES
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def is_widget_reply(content: str) -> bool:
    """True if ``content`` looks like a zform button-click reply.

    Used by the adapter to bypass the mention_only gate. Public so the adapter
    can import it.
    """
    if not content:
        return False
    head = content.lstrip()
    return any(head.startswith(p) for p in _widget_reply_prefixes())


def _build_zform_widget_content(heading: str, choices: list[dict]) -> dict:
    """Build the ``widget_content`` payload for a zform multiple-choice widget.

    Each choice: ``{"label": str, "reply": str}``. We translate to Zulip's
    internal schema (``short_name``, ``long_name``, ``reply``, ``type``).
    Short names are auto-assigned A, B, C, … (zform uses them as the "tag"
    label rendered next to the button text).
    """
    LETTERS = "ABCDEFGHIJ"  # max 10 enforced by schema
    out_choices = []
    for i, ch in enumerate(choices):
        out_choices.append({
            "type": "multiple_choice",
            "short_name": LETTERS[i] if i < len(LETTERS) else str(i + 1),
            "long_name": str(ch.get("label", "")).strip() or LETTERS[i],
            "reply": str(ch.get("reply", "")).strip(),
        })
    return {
        "widget_type": "zform",
        "extra_data": {
            "type": "choices",
            "heading": heading,
            "choices": out_choices,
        },
    }


def _build_fallback_text(heading: str, choices: list[dict]) -> str:
    """Auto-render a readable text fallback for non-web clients."""
    lines = [heading.strip(), ""]
    for i, ch in enumerate(choices):
        tag = "ABCDEFGHIJ"[i] if i < 10 else str(i + 1)
        label = str(ch.get("label", "")).strip()
        reply = str(ch.get("reply", "")).strip()
        lines.append(f"  **{tag}.** {label}  — reply with `{reply}`")
    lines.append("")
    lines.append("_(Click a button in the Zulip web/desktop app, or send "
                 "the reply text manually.)_")
    return "\n".join(lines)


async def _handle_zulip_buttons(args: dict, **_kwargs: Any) -> dict[str, Any]:
    stream = (args.get("stream") or "").strip()
    topic = (args.get("topic") or "").strip()
    dm_to = args.get("dm_to") or []
    heading = (args.get("heading") or "").strip()
    choices = args.get("choices") or []
    fallback_text = args.get("fallback_text")

    if not heading:
        return _err("heading is required")
    if not choices or not isinstance(choices, list):
        return _err("choices must be a non-empty list")
    if len(choices) > 10:
        return _err("max 10 choices supported by Zulip's zform widget")
    for i, ch in enumerate(choices):
        if not isinstance(ch, dict):
            return _err(f"choices[{i}] must be an object with label + reply")
        if not ch.get("label") or not ch.get("reply"):
            return _err(f"choices[{i}] missing label or reply")

    # Routing: stream+topic XOR dm_to
    use_stream = bool(stream)
    use_dm = bool(dm_to)
    if use_stream == use_dm:
        return _err("provide either (stream+topic) OR dm_to, not both / neither")
    if use_stream and not topic:
        return _err("topic is required when stream is set")

    widget_content = _build_zform_widget_content(heading, choices)
    content = (
        fallback_text
        if isinstance(fallback_text, str) and fallback_text.strip()
        else _build_fallback_text(heading, choices)
    )

    c = _client()
    if c is None:
        return _err("ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
    async with c:
        try:
            if use_stream:
                mid = await c.send_stream_message(
                    stream, topic, content, widget_content=widget_content,
                )
            else:
                mid = await c.send_direct_message(
                    list(dm_to), content, widget_content=widget_content,
                )
        except ZulipAPIError as e:
            return _err(str(e))

    out: dict[str, Any] = {"success": True, "message_id": mid}
    if use_stream:
        site = os.getenv("ZULIP_SITE", "").rstrip("/")
        from urllib.parse import quote
        out["url"] = (
            f"{site}/#narrow/stream/{quote(stream, safe='')}"
            f"/topic/{quote(topic, safe='')}/near/{mid}"
        )
    return out


_TOOLS = (
    ("zulip_post",               ZULIP_POST_SCHEMA,               _handle_zulip_post,               "💬"),
    ("zulip_dm",                 ZULIP_DM_SCHEMA,                 _handle_zulip_dm,                 "📩"),
    ("zulip_list_streams",       ZULIP_LIST_STREAMS_SCHEMA,       _handle_zulip_list_streams,       "📋"),
    ("zulip_list_topics",        ZULIP_LIST_TOPICS_SCHEMA,        _handle_zulip_list_topics,        "🧵"),
    ("zulip_upload_image",       ZULIP_UPLOAD_IMAGE_SCHEMA,       _handle_zulip_upload_image,       "🖼️"),
    ("zulip_react",              ZULIP_REACT_SCHEMA,              _handle_zulip_react,              "✨"),
    ("zulip_edit",               ZULIP_EDIT_SCHEMA,               _handle_zulip_edit,               "✏️"),
    ("zulip_delete",             ZULIP_DELETE_SCHEMA,             _handle_zulip_delete,             "🗑️"),
    ("zulip_fetch",              ZULIP_FETCH_SCHEMA,              _handle_zulip_fetch,              "📜"),
    ("zulip_create_channel",     ZULIP_CREATE_CHANNEL_SCHEMA,     _handle_zulip_create_channel,     "📢"),
    ("zulip_invite_to_channel",  ZULIP_INVITE_TO_CHANNEL_SCHEMA,  _handle_zulip_invite_to_channel,  "➕"),
    ("zulip_unsubscribe",        ZULIP_UNSUBSCRIBE_SCHEMA,        _handle_zulip_unsubscribe,        "🚪"),
    ("zulip_archive_channel",    ZULIP_ARCHIVE_CHANNEL_SCHEMA,    _handle_zulip_archive_channel,    "📦"),
    ("zulip_list_users",         ZULIP_LIST_USERS_SCHEMA,         _handle_zulip_list_users,         "👥"),
    ("zulip_buttons",            ZULIP_BUTTONS_SCHEMA,            _handle_zulip_buttons,            "🔘"),
)


def _register_composite_toolset() -> None:
    """Register ``hermes-zulip`` as a composite toolset.

    Without this, gateway-spawned Zulip sessions only get the 9 Zulip-specific
    tools (because Hermes' ``get_toolset()`` finds an auto-built registry entry
    for ``hermes-zulip`` and returns it before reaching the
    ``hermes-<platform>``-aware fallback that would have merged in
    ``_HERMES_CORE_TOOLS``). The user sees an agent without ``cronjob``,
    ``terminal``, ``web_search``, ``memory``, etc.

    Sister platforms (``hermes-telegram``, ``hermes-matrix``, …) are declared as
    explicit composites in the core ``toolsets.py``. Plugin platforms have to
    declare it themselves, which we do here.

    Best-effort: if the core import fails we log and continue — the plugin's
    Zulip-specific tools still register fine, just without the core bundle.
    """
    try:
        from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
    except Exception:
        logger.warning(
            "[zulip] could not import core toolsets — hermes-zulip will only "
            "expose Zulip-specific tools (no cron/terminal/web/memory). "
            "Add 'zulip: [hermes-cli, hermes-zulip]' to platform_toolsets in "
            "~/.hermes/config.yaml to work around."
        )
        return

    zulip_tool_names = [name for name, *_ in _TOOLS]
    TOOLSETS["hermes-zulip"] = {
        "description": (
            "Zulip platform toolset — full core CLI tools "
            "(cron, terminal, web, memory, …) plus Zulip-specific tools."
        ),
        "tools": sorted(set(_HERMES_CORE_TOOLS) | set(zulip_tool_names)),
        "includes": [],
    }
    logger.info(
        "[zulip] registered composite toolset 'hermes-zulip' with %d tools "
        "(%d core + %d zulip-specific)",
        len(TOOLSETS["hermes-zulip"]["tools"]),
        len(_HERMES_CORE_TOOLS),
        len(zulip_tool_names),
    )


def register_tools(ctx) -> None:
    """Register all Zulip tools. Called from adapter.register()."""
    # First: declare the composite toolset so gateway sessions get core tools.
    _register_composite_toolset()

    # Then: register each Zulip-specific tool into the same toolset name.
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
