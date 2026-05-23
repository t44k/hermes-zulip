"""Zulip Platform Adapter for Hermes Agent.

Maps Zulip's stream/topic model onto Hermes's chat_id/thread_id model:
  * Zulip stream  →  SessionSource.parent_chat_id = "stream:<name>",
                     SessionSource.chat_id        = "stream:<name>"
  * Zulip topic   →  SessionSource.thread_id      = <topic>
  * Zulip DM      →  SessionSource.chat_id        = "dm:<sorted user emails>"

M1 scope:
  * connect() — probe /users/me, list subscriptions, cache identity
  * send()    — text to "stream:<name>" with thread_id=topic
  * get_chat_info(), send_typing() stubs
  * register() with platform_hint, env enablement, manifest hookup

Later milestones add: event loop, reactions, uploads, editing, standalone sender.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy imports — main Hermes modules might not be on path during plugin discovery
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource

from .client import ZulipClient, ZulipAPIError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLATFORM_KEY = "zulip"
MAX_MESSAGE_LENGTH = 10000  # Zulip's documented per-message limit
DEFAULT_TOPIC = "(no topic)"

# Markdown attachment pattern Zulip emits when a user pastes/uploads a file:
#   [filename.png](/user_uploads/2/ab/cd/filename.png)
# We extract these from the message ``content`` to drive inbound media handling.
_USER_UPLOAD_LINK_RE = re.compile(r"\[([^\]]*)\]\((/user_uploads/[^\s)]+)\)")

# Image extensions we promote to MessageType.PHOTO (everything else stays DOCUMENT).
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"})


def _parse_user_uploads(content: str) -> List[Tuple[str, str]]:
    """Extract (filename, uri) attachment links from a Zulip message body."""
    return [(m.group(1) or "attachment", m.group(2)) for m in _USER_UPLOAD_LINK_RE.finditer(content or "")]

# Bad-queue error code returned when our event queue has expired (~10 min idle).
# We catch this in the event loop and re-register from scratch.
BAD_EVENT_QUEUE_CODE = "BAD_EVENT_QUEUE_ID"

# Reconnect backoff (seconds). Exponential with jitter, capped.
_RECONNECT_BACKOFF_MIN = 1.0
_RECONNECT_BACKOFF_MAX = 60.0

PLATFORM_HINT = (
    "You are on Zulip, a chat platform organised into **streams** "
    "(project-level channels) and **topics** (threaded conversations inside a stream). "
    "Each topic is its own conversation with its own context — when the user shifts "
    "to a new subject that doesn't fit the current topic, open a new topic in the "
    "same stream by calling `zulip_post(stream=<current_stream>, topic=<new-name>, "
    "content=<your message>)`. Topics are auto-created on first post — no setup "
    "step required. Prefer short kebab-case topic names (≤60 chars). "
    "When you're unsure which topics already exist, call `zulip_list_topics(stream)`. "
    "Reply in the existing topic when continuing the same discussion. "
    "**Incoming messages are prefixed with `[msg #<id>] …`** — that integer "
    "is the Zulip message_id, the only reliable way to target the message "
    "for reactions, edits, or quoted replies. Use it; do NOT guess IDs. "
    "Strip the `[msg #N]` prefix from the user's text before reasoning about "
    "the content. Your own outbound messages are also auto-tagged with "
    "`[msg #N]` after sending, so when you scroll back via `zulip_fetch` you "
    "can identify your own past posts. "
    "To read the conversation history of a topic or DM (e.g. to summarise, "
    "quote, edit, or delete an earlier message you don't see in this turn), "
    "call `zulip_fetch(stream=..., topic=..., num_before=N)` or "
    "`zulip_fetch(anchor=<id>, num_before=N, num_after=M)`. "
    "Zulip Markdown is supported: **bold**, *italic*, `code`, ```code blocks```, "
    "tables, spoilers (||spoiler||), and @-mentions like @**Tamas**. "
    "Messages can be edited; long streamed responses update in place. "
    "When you spot a typo or factual error in one of your own recent "
    "messages, prefer `zulip_edit(message_id, content=...)` over sending a "
    "follow-up correction — the bot's previous message_id is returned by "
    "`zulip_post` and tagged on inbound edit events. Use "
    "`zulip_delete(message_id)` to retract a wrong message entirely. When a "
    "user edits one of their own messages you receive a synthetic event "
    "`[<name> edited msg #N → <new text>]` — treat the new text as the "
    "authoritative version and reply (or edit your prior reply) accordingly. "
    "For lightweight acknowledgements (e.g. confirming you saw a request "
    "before doing the work), call `zulip_react(message_id, emoji_name)` "
    "instead of posting a one-word reply. Common short names: thumbs_up, "
    "tada, eyes, check, sparkles. When the user reacts to one of your "
    "messages, you receive a synthetic event `[<name> reacted :emoji: …]` — "
    "treat it as a soft signal and only respond if it clearly invites a reply."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_chat_id(chat_id: str) -> tuple[str, str, Optional[str]]:
    """Parse a Hermes chat_id into (kind, value, embedded_topic).

    Accepts:
      ``stream:<name>``              →  ("stream", "<name>", None)
      ``stream:<name>:<topic>``      →  ("stream", "<name>", "<topic>")
                                        — used by cron's
                                        ``--deliver zulip:stream:foo:bar``
      ``dm:<a@x,b@y>``               →  ("dm", "<a@x,b@y>", None)
      bare ``<name>``                →  ("stream", "<name>", None)
                                        (lenient default)
    """
    if ":" in chat_id:
        kind, _, val = chat_id.partition(":")
        if kind == "stream":
            # Allow an embedded topic for cron-style targets.
            if ":" in val:
                stream, _, topic = val.partition(":")
                return "stream", stream, topic or None
            return "stream", val, None
        if kind == "dm":
            return "dm", val, None
    return "stream", chat_id, None


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ZulipAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_KEY))
        extra = config.extra or {}

        self.site: str = extra.get("site") or os.getenv("ZULIP_SITE", "")
        self.email: str = extra.get("email") or os.getenv("ZULIP_EMAIL", "")
        self.api_key: str = (
            extra.get("api_key")
            or config.token
            or os.getenv("ZULIP_API_KEY", "")
        )
        self.home_channel: str = (
            extra.get("home_channel") or os.getenv("ZULIP_HOME_CHANNEL", "")
        )
        self.verify_tls: bool = _truthy(
            extra.get("verify_tls", os.getenv("ZULIP_VERIFY_TLS", "true"))
        )
        self.auto_create_topics: bool = _truthy(
            extra.get("auto_create_topics", os.getenv("ZULIP_AUTO_CREATE_TOPICS", "true"))
        )
        # ---- M6 polish: seen / done eye-reactions ----
        # When enabled, every inbound message gets a :seen_emoji: reaction
        # the moment it arrives; the reaction is removed when the agent's
        # background turn task completes (success OR failure — leaving the
        # eye on a crash is a clearer signal than silently removing it on
        # an empty/error reply, so we *only* clean up on successful turn
        # completion via the done-callback's exception check).
        self.auto_seen_reaction: bool = _truthy(
            extra.get("auto_seen_reaction", os.getenv("ZULIP_AUTO_SEEN_REACTION", "true"))
        )
        self.seen_emoji: str = (
            extra.get("seen_emoji") or os.getenv("ZULIP_SEEN_EMOJI", "eyes")
        ).lstrip(":").rstrip(":").strip() or "eyes"
        # ---- M9: tag outbound messages with their own [msg #N] prefix ----
        # When the bot scrolls back through a thread via zulip_fetch, the
        # only way it can identify its OWN past messages is if their ids
        # are baked into the content. After every successful send we
        # PATCH the just-posted message to prepend "[msg #N] " (mirroring
        # the inbound prefix). Single extra round-trip; self-edit event
        # is filtered upstream so this doesn't cause feedback loops.
        self.tag_outgoing_ids: bool = _truthy(
            extra.get("tag_outgoing_ids", os.getenv("ZULIP_TAG_OUTGOING_IDS", "true"))
        )

        self._client: Optional[ZulipClient] = None
        self._me: dict = {}
        self._streams_by_name: dict[str, dict] = {}
        self._streams_by_id: dict[int, dict] = {}

        # Event-queue state
        self._queue_id: Optional[str] = None
        self._last_event_id: int = -1
        self._event_task: Optional[asyncio.Task] = None
        self._stopping: asyncio.Event = asyncio.Event()

    # ---------- lifecycle -------------------------------------------------

    async def connect(self) -> bool:
        if not (self.site and self.email and self.api_key):
            logger.error(
                "[zulip] missing credentials: site=%s email=%s api_key=%s",
                bool(self.site), bool(self.email), bool(self.api_key),
            )
            return False
        try:
            self._client = ZulipClient(
                self.site, self.email, self.api_key,
                verify_tls=self.verify_tls,
            )
            await self._client.connect()
            self._me = await self._client.get_me()
            await self._refresh_streams()
            logger.info(
                "[zulip] connected as %s (%s) — %d subscribed streams",
                self._me.get("full_name"),
                self._me.get("email"),
                len(self._streams_by_name),
            )

            # Register event queue and spawn loop
            await self._register_queue()
            self._stopping.clear()
            self._event_task = asyncio.create_task(
                self._event_loop(), name="zulip-event-loop",
            )
            return True
        except ZulipAPIError as e:
            logger.error("[zulip] connect failed: %s", e)
            return False
        except Exception:
            logger.exception("[zulip] connect crashed")
            return False

    async def disconnect(self) -> None:
        self._stopping.set()
        if self._event_task is not None:
            self._event_task.cancel()
            try:
                await self._event_task
            except (asyncio.CancelledError, Exception):
                pass
            self._event_task = None

        if self._client is not None and self._queue_id is not None:
            try:
                await self._client.delete_event_queue(self._queue_id)
            except Exception:
                logger.debug("[zulip] error deleting event queue", exc_info=True)
        self._queue_id = None

        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.exception("[zulip] error during disconnect")
        self._client = None

    # ---------- event queue ----------------------------------------------

    async def _refresh_streams(self) -> None:
        assert self._client is not None
        subs = await self._client.get_subscriptions()
        self._streams_by_name = {s["name"]: s for s in subs}
        self._streams_by_id = {int(s["stream_id"]): s for s in subs}

    async def _register_queue(self) -> None:
        """Open a fresh event queue.  We listen for messages + reactions + updates."""
        assert self._client is not None
        resp = await self._client.register_event_queue(
            event_types=["message", "update_message", "reaction"],
        )
        self._queue_id = resp["queue_id"]
        self._last_event_id = int(resp.get("last_event_id", -1))
        logger.info(
            "[zulip] event queue registered: id=%s last_event_id=%s",
            self._queue_id, self._last_event_id,
        )

    async def _event_loop(self) -> None:
        """Long-poll events forever; reconnect with backoff on errors."""
        backoff = _RECONNECT_BACKOFF_MIN
        while not self._stopping.is_set():
            try:
                assert self._client is not None and self._queue_id is not None
                events = await self._client.get_events(
                    self._queue_id, self._last_event_id,
                )
                # Success: reset backoff
                backoff = _RECONNECT_BACKOFF_MIN
                for ev in events:
                    self._last_event_id = max(self._last_event_id, int(ev.get("id", -1)))
                    try:
                        await self._handle_event(ev)
                    except Exception:
                        logger.exception("[zulip] error handling event %s", ev.get("type"))
            except asyncio.CancelledError:
                break
            except ZulipAPIError as e:
                if e.code == BAD_EVENT_QUEUE_CODE:
                    logger.warning("[zulip] event queue expired; re-registering")
                    try:
                        await self._register_queue()
                        backoff = _RECONNECT_BACKOFF_MIN
                        continue
                    except Exception:
                        logger.exception("[zulip] re-register failed; backing off")
                else:
                    logger.error("[zulip] events API error: %s", e)
                await self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
            except Exception:
                logger.exception("[zulip] event loop transient error")
                await self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
                # On any unknown crash, try to refresh the queue.
                try:
                    if self._client is not None:
                        await self._register_queue()
                except Exception:
                    logger.debug("[zulip] post-error re-register failed", exc_info=True)
        logger.info("[zulip] event loop stopped")

    async def _sleep_with_jitter(self, base: float) -> None:
        delay = base + random.uniform(0, base * 0.5)
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass  # normal — backoff elapsed

    async def _handle_event(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "message":
            await self._handle_message_event(ev["message"])
        elif etype == "update_message":
            await self._handle_update_message_event(ev)
        elif etype == "reaction":
            await self._handle_reaction_event(ev)
        elif etype == "subscription":
            # Refresh our stream cache opportunistically
            try:
                await self._refresh_streams()
            except Exception:
                pass

    async def _handle_message_event(self, m: dict) -> None:
        """Dispatch one Zulip message to Hermes via self.handle_message()."""
        sender_id = m.get("sender_id")
        my_id = self._me.get("user_id")
        if sender_id is not None and my_id is not None and int(sender_id) == int(my_id):
            return  # self-filter

        msg_type = m.get("type", "stream")
        if msg_type == "stream":
            stream_name = m.get("display_recipient") or ""
            topic = (m.get("subject") or "").strip() or DEFAULT_TOPIC
            chat_id = f"stream:{stream_name}"
            parent_chat_id = chat_id
            thread_id = topic
            chat_name = f"#{stream_name}"
            chat_type = "channel"
            stream_descr = ""
            sid = m.get("stream_id")
            if sid is not None:
                stream_descr = (self._streams_by_id.get(int(sid)) or {}).get("description", "")
        else:
            # direct / huddle: build a stable chat_id from the sorted set of
            # sender + recipient user ids.
            recipients = m.get("display_recipient") or []
            user_ids = sorted({int(r["id"]) for r in recipients if "id" in r}) if isinstance(recipients, list) else []
            if my_id is not None and int(my_id) not in user_ids:
                user_ids = sorted(user_ids + [int(my_id)])
            chat_id = "dm:" + ",".join(str(u) for u in user_ids)
            parent_chat_id = None
            thread_id = None
            chat_name = "Zulip DM"
            chat_type = "dm"
            stream_descr = ""

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(m.get("sender_id", "")),
            user_name=m.get("sender_full_name") or m.get("sender_email") or "unknown",
            thread_id=thread_id,
            chat_topic=stream_descr or None,
            parent_chat_id=parent_chat_id,
            message_id=str(m.get("id")),
        )

        content = m.get("content", "") or ""
        # ---- M5: inbound media ------------------------------------------
        # Zulip embeds attachments as `[name](/user_uploads/...)` markdown.
        # Download each, cache locally, and expose as media_urls. Promote the
        # event type to PHOTO when *all* attachments are images; otherwise
        # leave it as TEXT and let downstream tools open the cached paths.
        media_urls: List[str] = []
        media_types: List[str] = []
        uploads = _parse_user_uploads(content)
        msg_type = MessageType.TEXT
        for filename, uri in uploads:
            try:
                data = await self._client.download_user_upload(uri)
            except Exception:
                logger.exception("[zulip] failed to download attachment %s", uri)
                continue
            ext = Path(filename).suffix.lower() or ".bin"
            mime, _ = mimetypes.guess_type(filename)
            if ext in _IMAGE_EXTS:
                cached = cache_image_from_bytes(data, ext=ext)
                media_urls.append(cached)
                media_types.append(mime or "image/png")
            else:
                cached = cache_document_from_bytes(data, filename=filename)
                media_urls.append(cached)
                media_types.append(mime or "application/octet-stream")
        if media_urls and all(t.startswith("image/") for t in media_types):
            msg_type = MessageType.PHOTO

        # Prefix the message text with the Zulip message id so the agent can
        # reliably target it for reactions, edits, and quoted replies without
        # having to guess. Reaction events do the same with their #<id> tail.
        # See PLATFORM_HINT for the contract.
        zulip_msg_id = m.get("id")
        if zulip_msg_id is not None:
            display_text = f"[msg #{zulip_msg_id}] {content}" if content else f"[msg #{zulip_msg_id}]"
        else:
            display_text = content

        event = MessageEvent(
            text=display_text,
            message_type=msg_type,
            source=source,
            raw_message=m,
            message_id=str(m.get("id")),
            media_urls=media_urls,
            media_types=media_types,
        )
        await self.handle_message(event)

    # ---------- M6 polish: "seen / done" eye-reaction lifecycle ----------

    async def handle_message(self, event: MessageEvent) -> None:  # type: ignore[override]
        """Wrap the base handler with an inbound-message "seen / done"
        reaction lifecycle.

        Flow:
          1. Add :eyes: to the inbound message immediately (so the user
             knows the bot saw their message even before the LLM responds).
          2. Delegate to ``super().handle_message`` which spawns the agent
             turn as a background task.
          3. Hook the background task's completion via add_done_callback so
             the reaction is removed on success and *left in place* on
             exception (clearer signal — eye lingers if the bot crashed).

        Skipped when ``auto_seen_reaction`` is False, on synthetic reaction
        events (message_id like ``reaction:N:emoji``), and when the message
        was authored by the bot itself (no double-add).
        """
        if not (self.auto_seen_reaction and self._client):
            await super().handle_message(event)
            return

        # Only attempt on numeric Zulip message ids — reaction events embed
        # their own marker ("reaction:<id>:<emoji>") and shouldn't get eyes.
        raw_mid = event.message_id
        try:
            zulip_mid = int(raw_mid) if raw_mid is not None else None
        except (TypeError, ValueError):
            zulip_mid = None
        if zulip_mid is None:
            await super().handle_message(event)
            return

        emoji = self.seen_emoji
        # Add :eyes: first (best-effort — never block dispatch on a reaction
        # failure; surface to debug log).
        try:
            await self._client.add_reaction(zulip_mid, emoji)
        except Exception:
            logger.debug("[zulip] seen-reaction add failed for msg=%s", zulip_mid, exc_info=True)

        # Snapshot pre-dispatch session task set so we can identify the new
        # task this event spawns. The base implementation creates the task
        # synchronously inside handle_message → _start_session_processing,
        # so it's available immediately after the await returns.
        before = set(self._session_tasks.values())
        try:
            await super().handle_message(event)
        except Exception:
            # Dispatch itself failed — remove the eye since no turn will run.
            logger.exception("[zulip] super().handle_message raised; clearing :%s:", emoji)
            asyncio.create_task(self._remove_seen_reaction(zulip_mid, emoji))
            raise

        new_tasks = [t for t in self._session_tasks.values() if t not in before]
        if not new_tasks:
            # No background task spawned (e.g. inline command dispatch path,
            # or duplicate-event filtered upstream). Remove the eye now —
            # the message has already been fully processed.
            asyncio.create_task(self._remove_seen_reaction(zulip_mid, emoji))
            return

        task = new_tasks[-1]

        def _on_turn_done(t: "asyncio.Task[Any]", _mid: int = zulip_mid, _emoji: str = emoji) -> None:
            exc = None
            try:
                exc = t.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                exc = None
            if exc is not None:
                # Leave the eye in place to signal "saw, didn't finish".
                logger.warning(
                    "[zulip] turn for msg=%s ended with %s — leaving :%s: in place",
                    _mid, type(exc).__name__, _emoji,
                )
                return
            # Schedule removal on the running loop.
            loop = asyncio.get_event_loop()
            loop.create_task(self._remove_seen_reaction(_mid, _emoji))

        try:
            task.add_done_callback(_on_turn_done)
        except Exception:
            # Some tests use plain object() sentinels — fall back to immediate
            # removal so we don't leave the eye orphaned in synthetic flows.
            asyncio.create_task(self._remove_seen_reaction(zulip_mid, emoji))

    async def _remove_seen_reaction(self, msg_id: int, emoji: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.remove_reaction(msg_id, emoji)
        except Exception:
            logger.debug(
                "[zulip] seen-reaction remove failed for msg=%s emoji=%s",
                msg_id, emoji, exc_info=True,
            )

    async def _handle_update_message_event(self, ev: dict) -> None:
        """Surface a user-initiated message edit as a synthetic agent event.

        Zulip ``update_message`` event payload (relevant fields)::

            {type: "update_message", message_id, user_id,
             orig_content, content, rendered_content,
             stream_id, orig_subject, subject, propagate_mode, ...}

        Behaviour:
          * Skip our own edits (``user_id == bot``) — those are echoes of
            ``zulip_edit`` tool calls and would cause feedback loops.
          * Skip topic-only renames (no ``content`` change) — noisy and not
            actionable for the agent.
          * Look up the target message to recover stream/topic/author so the
            edit lands in the same Hermes session as the original.
          * Dispatch a MessageEvent whose text is
            ``[<user> edited msg #N → <new content>]``.
        """
        op_user = ev.get("user_id")
        my_id = self._me.get("user_id")
        if op_user is not None and my_id is not None and int(op_user) == int(my_id):
            return  # self-edit echo

        new_content = ev.get("content")
        if new_content is None:
            # Pure topic move / metadata-only edit — ignore.
            return

        msg_id = ev.get("message_id")
        if msg_id is None or self._client is None:
            return

        try:
            target = await self._client.get_message(int(msg_id))
        except Exception:
            logger.warning("[zulip] update_message: could not fetch message %s", msg_id)
            return

        # Route based on the (possibly-new) stream/topic of the edited message.
        msg_type = target.get("type", "stream")
        if msg_type == "stream":
            stream_name = target.get("display_recipient") or ""
            topic = (target.get("subject") or "").strip() or DEFAULT_TOPIC
            chat_id = f"stream:{stream_name}"
            parent_chat_id = chat_id
            thread_id = topic
            chat_name = f"#{stream_name}"
            chat_type = "channel"
        else:
            recipients = target.get("display_recipient") or []
            user_ids = sorted({int(r["id"]) for r in recipients if "id" in r}) if isinstance(recipients, list) else []
            if my_id is not None and int(my_id) not in user_ids:
                user_ids = sorted(user_ids + [int(my_id)])
            chat_id = "dm:" + ",".join(str(u) for u in user_ids)
            parent_chat_id = None
            thread_id = None
            chat_name = "Zulip DM"
            chat_type = "dm"

        editor_name = (
            target.get("sender_full_name")
            if target.get("sender_id") == op_user
            else f"user:{op_user}"
        )
        # The Zulip event payload doesn't include the editor's name directly
        # (only user_id). Fall back to the original sender's name when the
        # edit was made by the author themselves; otherwise label by id.
        if op_user is not None and target.get("sender_id") != op_user:
            editor_name = f"user:{op_user}"
        elif not editor_name:
            editor_name = f"user:{op_user}"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(op_user) if op_user is not None else "",
            user_name=editor_name,
            thread_id=thread_id,
            parent_chat_id=parent_chat_id,
            message_id=f"edit:{msg_id}",
        )

        # Truncate the rebroadcast text — the agent doesn't need a 10KB diff
        # spelled out, and the prefix makes the intent clear.
        snippet = new_content if len(new_content) <= 2000 else new_content[:2000] + "…"
        synthetic_text = f"[{editor_name} edited msg #{msg_id} → {snippet}]"

        event = MessageEvent(
            text=synthetic_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={"update_message_event": ev, "target_message": target},
            message_id=f"edit:{msg_id}",
        )
        logger.info(
            "[zulip] edit on msg %s by user=%s — dispatched to session",
            msg_id, op_user,
        )
        await self.handle_message(event)

    async def _handle_reaction_event(self, ev: dict) -> None:
        """Surface a user's reaction to the agent as a synthetic text message.

        Zulip's reaction event payload:
          {type: "reaction", op: "add"|"remove", message_id, emoji_name,
           emoji_code, reaction_type, user_id, user: {...}}

        We:
          * skip our own reactions (echoes of zulip_react tool calls)
          * skip "remove" events (noisy; agent only needs the add signal)
          * look up the message to recover stream/topic for session routing
          * skip reactions on messages NOT authored by the bot (avoid noise
            on the user reacting to their own message or a third-party's)
          * dispatch as a MessageEvent with text "<user> reacted :emoji: to
            your message" — landing in the right session by message context
        """
        op = ev.get("op")
        if op != "add":
            return
        sender_id = ev.get("user_id")
        if sender_id is None and isinstance(ev.get("user"), dict):
            sender_id = ev["user"].get("user_id") or ev["user"].get("id")
        my_id = self._me.get("user_id")
        if sender_id is not None and my_id is not None and int(sender_id) == int(my_id):
            return  # self-reaction echo

        msg_id = ev.get("message_id")
        emoji_name = ev.get("emoji_name") or "?"
        if msg_id is None or self._client is None:
            return

        # Look up the target message to recover stream/topic + author.
        try:
            target = await self._client.get_message(int(msg_id))
        except Exception:
            logger.warning("[zulip] reaction: could not fetch message %s", msg_id)
            return

        target_author = target.get("sender_id")
        if my_id is None or target_author != my_id:
            # User reacted to someone else's message — out of scope for the
            # agent's session. Log and skip.
            logger.debug(
                "[zulip] reaction :%s: from user=%s on msg=%s (author=%s, not me) — ignored",
                emoji_name, sender_id, msg_id, target_author,
            )
            return

        # Route into the same session the bot's original reply belonged to.
        msg_type = target.get("type", "stream")
        if msg_type == "stream":
            stream_name = target.get("display_recipient") or ""
            topic = (target.get("subject") or "").strip() or DEFAULT_TOPIC
            chat_id = f"stream:{stream_name}"
            parent_chat_id = chat_id
            thread_id = topic
            chat_name = f"#{stream_name}"
            chat_type = "channel"
        else:
            recipients = target.get("display_recipient") or []
            user_ids = sorted({int(r["id"]) for r in recipients if "id" in r}) if isinstance(recipients, list) else []
            if my_id is not None and int(my_id) not in user_ids:
                user_ids = sorted(user_ids + [int(my_id)])
            chat_id = "dm:" + ",".join(str(u) for u in user_ids)
            parent_chat_id = None
            thread_id = None
            chat_name = "Zulip DM"
            chat_type = "dm"

        user_info = ev.get("user") or {}
        user_name = (
            user_info.get("full_name")
            or user_info.get("email")
            or f"user:{sender_id}"
        )

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(sender_id) if sender_id is not None else "",
            user_name=user_name,
            thread_id=thread_id,
            parent_chat_id=parent_chat_id,
            message_id=f"reaction:{msg_id}:{emoji_name}",
        )

        synthetic_text = f"[{user_name} reacted :{emoji_name}: to your message #{msg_id}]"
        event = MessageEvent(
            text=synthetic_text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={"reaction_event": ev, "target_message": target},
            message_id=f"reaction:{msg_id}:{emoji_name}",
        )
        logger.info(
            "[zulip] reaction :%s: from %s on bot msg %s — dispatched to session",
            emoji_name, user_name, msg_id,
        )
        await self.handle_message(event)

    # ---------- outbound --------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str = "",
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        text: Optional[str] = None,
        thread_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> SendResult:
        # Accept both the gateway's standard (chat_id, content, reply_to, metadata)
        # signature and the legacy (chat_id, text=, thread_id=) keyword form used
        # by smoke tests / standalone scripts.
        body = text if text is not None else content
        if thread_id is None and metadata:
            thread_id = metadata.get("thread_id") or metadata.get("topic")
        if not self._client:
            return SendResult(success=False, error="zulip: not connected")
        kind, val, embedded_topic = _parse_chat_id(chat_id)
        # Explicit thread_id / metadata.thread_id wins; embedded
        # `stream:foo:bar` topic is the fallback for cron-style targets.
        if not thread_id and embedded_topic:
            thread_id = embedded_topic
        try:
            if kind == "stream":
                topic = thread_id or DEFAULT_TOPIC
                msg_id = await self._client.send_stream_message(val, topic, body)
            else:  # dm
                recipients = [e.strip() for e in val.split(",") if e.strip()]
                msg_id = await self._client.send_direct_message(recipients, body)
            # M9: prepend [msg #N] so the bot can identify its own messages
            # when scrolling history via zulip_fetch. Skip if the body already
            # carries the prefix (e.g. the agent included it manually), or if
            # the post was an internal helper that turned around and posted a
            # bare ID marker.
            if (
                self.tag_outgoing_ids
                and body
                and not body.lstrip().startswith("[msg #")
            ):
                tagged = f"[msg #{msg_id}] {body}"
                try:
                    await self._client.update_message(int(msg_id), content=tagged)
                except Exception:
                    logger.debug(
                        "[zulip] outgoing-id tag failed for msg=%s (non-fatal)",
                        msg_id, exc_info=True,
                    )
            return SendResult(success=True, message_id=str(msg_id))
        except ZulipAPIError as e:
            logger.error("[zulip] send failed: %s", e)
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.exception("[zulip] send crashed")
            return SendResult(success=False, error=str(e))

    # M10: streaming-response in-place edits ------------------------------
    # The gateway's GatewayStreamConsumer drives token-by-token streaming
    # by calling adapter.edit_message() in a tight loop. The first chunk
    # goes through normal send(); every subsequent chunk lands here.
    # We re-apply the [msg #N] prefix on each edit so the tag isn't lost
    # as the body grows, and we filter out the synthetic update_message
    # event that Zulip emits for each PATCH (so the agent doesn't see its
    # own streaming edits as user-edits coming back in).
    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> SendResult:
        """Edit an existing Zulip message in place.

        Called by GatewayStreamConsumer between deltas during a streamed
        response, and (with ``finalize=True``) once at the end to commit
        the final state.

        Re-applies the ``[msg #N]`` auto-tag prefix when enabled so the
        ID stays visible after every edit.
        """
        if not self._client:
            return SendResult(success=False, error="zulip: not connected")
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return SendResult(success=False, error=f"zulip: invalid message_id {message_id!r}")

        body = content or ""
        if (
            self.tag_outgoing_ids
            and body
            and not body.lstrip().startswith("[msg #")
        ):
            body = f"[msg #{mid}] {body}"

        try:
            await self._client.update_message(mid, content=body)
            return SendResult(success=True, message_id=str(mid))
        except ZulipAPIError as e:
            logger.debug("[zulip] edit_message failed for mid=%s: %s", mid, e)
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.exception("[zulip] edit_message crashed")
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, thread_id: Optional[str] = None) -> None:
        """Show a typing indicator to the user.

        Zulip auto-clears after ~15s, so we only send the ``start`` op.
        """
        if not self._client:
            return None
        kind, val, embedded_topic = _parse_chat_id(chat_id)
        if not thread_id and embedded_topic:
            thread_id = embedded_topic
        try:
            if kind == "stream":
                topic = thread_id or DEFAULT_TOPIC
                stream_id = (self._streams_by_name.get(val) or {}).get("stream_id")
                if stream_id is None:
                    return None
                await self._client._request(  # noqa: SLF001
                    "POST",
                    "/typing",
                    data={
                        "type": "stream",
                        "op": "start",
                        "stream_id": int(stream_id),
                        "topic": topic,
                    },
                )
            else:
                recipients = [e.strip() for e in val.split(",") if e.strip()]
                await self._client._request(  # noqa: SLF001
                    "POST",
                    "/typing",
                    data={"type": "direct", "op": "start", "to": recipients},
                )
        except Exception:
            logger.debug("[zulip] send_typing failed (non-fatal)", exc_info=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        thread_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> SendResult:
        """Send an image to a Zulip stream/topic or DM.

        ``image_url`` may be a local filesystem path or an http(s) URL. We
        upload the bytes to Zulip's ``/user_uploads`` endpoint so the image
        renders inline (rather than as a bare external link, which Zulip
        sometimes hot-links and sometimes refuses to preview).
        """
        if not self._client:
            return SendResult(success=False, error="zulip: not connected")
        # Read or fetch the bytes
        try:
            if image_url.startswith("http://") or image_url.startswith("https://"):
                import httpx
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as hc:
                    r = await hc.get(image_url)
                    if r.status_code != 200:
                        return SendResult(success=False, error=f"fetch {image_url} → {r.status_code}")
                    data = r.content
                filename = Path(image_url.split("?", 1)[0]).name or "image"
            else:
                p = Path(image_url).expanduser()
                if not p.exists():
                    return SendResult(success=False, error=f"file not found: {p}")
                data = p.read_bytes()
                filename = p.name
        except Exception as e:
            logger.exception("[zulip] send_image: read failed")
            return SendResult(success=False, error=f"read failed: {e}")

        mime, _ = mimetypes.guess_type(filename)
        try:
            uri = await self._client.upload_file(filename, data, mime or "image/png")
        except ZulipAPIError as e:
            return SendResult(success=False, error=f"upload failed: {e}")

        body = f"[{filename}]({uri})"
        if caption:
            body = f"{caption}\n\n{body}"
        return await self.send(chat_id, body, thread_id=thread_id)

    # ---------- introspection --------------------------------------------

    async def get_chat_info(self, chat_id: str) -> dict:
        kind, val, _embedded = _parse_chat_id(chat_id)
        if kind == "stream":
            s = self._streams_by_name.get(val) or {}
            return {
                "name": val,
                "type": "stream",
                "chat_id": chat_id,
                "description": s.get("description", ""),
            }
        return {"name": val, "type": "dm", "chat_id": chat_id}


# ---------------------------------------------------------------------------
# Plugin hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Return True if the plugin's runtime deps are importable."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        logger.warning("[zulip] httpx not installed — `pip install httpx`")
        return False
    return True


def validate_config(cfg: PlatformConfig) -> tuple[bool, str]:
    extra = cfg.extra or {}
    site = extra.get("site") or os.getenv("ZULIP_SITE")
    email = extra.get("email") or os.getenv("ZULIP_EMAIL")
    key = extra.get("api_key") or cfg.token or os.getenv("ZULIP_API_KEY")
    if not site:
        return False, "ZULIP_SITE is required (e.g. https://zulip.example.com)"
    if not email:
        return False, "ZULIP_EMAIL is required (bot email from Settings → Bots)"
    if not key:
        return False, "ZULIP_API_KEY is required (bot API key from Settings → Bots)"
    return True, ""


def is_connected(cfg: PlatformConfig | None = None) -> bool:
    """Cheap "configured?" check for `hermes gateway status`.

    Accepts an optional PlatformConfig (the registry passes one); falls back
    to env vars when called with no argument.
    """
    if cfg is not None:
        extra = cfg.extra or {}
        if extra.get("site") and extra.get("email") and (extra.get("api_key") or cfg.token):
            return True
    return all(os.getenv(v) for v in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY"))


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars before adapter construction.

    Returning a dict tells the platform registry that the plugin is
    "env-configured" — it shows up in `hermes gateway status` even before
    the adapter is instantiated.
    """
    if not is_connected():
        return None
    out: dict[str, Any] = {
        "site": os.getenv("ZULIP_SITE"),
        "email": os.getenv("ZULIP_EMAIL"),
        "api_key": os.getenv("ZULIP_API_KEY"),
        "home_channel": os.getenv("ZULIP_HOME_CHANNEL", ""),
        "verify_tls": os.getenv("ZULIP_VERIFY_TLS", "true"),
        "auto_create_topics": os.getenv("ZULIP_AUTO_CREATE_TOPICS", "true"),
    }
    return out


# ---------------------------------------------------------------------------
# Standalone sender (cron + send_message tool delivery outside the gateway)
# ---------------------------------------------------------------------------

async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
    **_kwargs: Any,
) -> dict:
    """Out-of-process send for cron jobs (and the ``send_message`` agent tool
    when no live gateway adapter is registered for this process).

    The gateway invokes us with the keyword args defined in
    ``tools/send_message_tool.py`` — ``thread_id`` (topic), ``media_files``
    (list of local paths), and ``force_document`` (don't auto-promote images
    to inline preview). We honour all three.

    Topic resolution order:
      1. Explicit ``thread_id`` kwarg (preferred — this is what cron passes).
      2. Trailing ``:<topic>`` segment in ``chat_id`` (legacy / smoke tests).
      3. ``DEFAULT_TOPIC``.

    ``chat_id`` formats accepted (matches the live adapter)::

        "stream:<name>"          → stream message, topic from thread_id
        "stream:<name>:<topic>"  → legacy; thread_id wins if provided
        "<name>"                 → lenient; treated as stream
        "dm:<email1>,<email2>"   → direct / group DM
    """
    extra = pconfig.extra or {}
    site = extra.get("site") or os.getenv("ZULIP_SITE", "")
    email = extra.get("email") or os.getenv("ZULIP_EMAIL", "")
    key = extra.get("api_key") or pconfig.token or os.getenv("ZULIP_API_KEY", "")
    verify_tls = _truthy(extra.get("verify_tls", os.getenv("ZULIP_VERIFY_TLS", "true")))
    tag_outgoing = _truthy(
        extra.get("tag_outgoing_ids", os.getenv("ZULIP_TAG_OUTGOING_IDS", "true"))
    )
    if not (site and email and key):
        return {"success": False, "error": "zulip: missing credentials"}

    parts = chat_id.split(":")
    is_dm = parts[0] == "dm" and len(parts) >= 2
    if is_dm:
        recipients = [p.strip() for p in parts[1].split(",") if p.strip()]
        stream = None
        topic = None
    else:
        if parts[0] == "stream":
            stream = parts[1] if len(parts) > 1 else ""
            legacy_topic = parts[2] if len(parts) > 2 else None
        elif len(parts) == 2:
            stream, legacy_topic = parts[0], parts[1]
        else:
            stream, legacy_topic = parts[0], None
        topic = (thread_id or legacy_topic or DEFAULT_TOPIC).strip() or DEFAULT_TOPIC
        if not stream:
            return {"success": False, "error": "zulip: empty stream in chat_id"}

    try:
        async with ZulipClient(site, email, key, verify_tls=verify_tls) as c:
            # ---- media upload (if any) --------------------------------
            body = message or ""
            uploaded_uris: list[tuple[str, str]] = []  # (filename, uri)
            for path_str in (media_files or []):
                p = Path(path_str).expanduser()
                if not p.exists():
                    logger.warning("[zulip] standalone: media file not found: %s", p)
                    continue
                try:
                    mime, _ = mimetypes.guess_type(p.name)
                    data = p.read_bytes()
                    uri = await c.upload_file(p.name, data, mime or "application/octet-stream")
                    uploaded_uris.append((p.name, uri))
                except Exception as e:
                    logger.warning("[zulip] standalone: upload failed for %s: %s", p, e)

            if uploaded_uris:
                # Always render as Zulip attachment markdown. Inline preview
                # is auto-decided by Zulip from MIME — force_document only
                # matters on platforms that distinguish inline-photo vs
                # file-attachment delivery; on Zulip both render the same.
                links = "\n".join(f"[{name}]({uri})" for name, uri in uploaded_uris)
                body = f"{body}\n\n{links}" if body else links

            # ---- send --------------------------------------------------
            if is_dm:
                mid = await c.send_direct_message(recipients, body)
            else:
                mid = await c.send_stream_message(stream, topic, body)

            # ---- [msg #N] auto-tag (mirrors live adapter) -------------
            if (
                tag_outgoing
                and body
                and not body.lstrip().startswith("[msg #")
            ):
                tagged = f"[msg #{mid}] {body}"
                try:
                    await c.update_message(int(mid), content=tagged)
                except Exception:
                    logger.debug(
                        "[zulip] standalone: tag failed for msg=%s",
                        mid, exc_info=True,
                    )

        return {"success": True, "message_id": str(mid)}
    except ZulipAPIError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("[zulip] standalone send crashed")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Called by the Hermes plugin loader at startup."""
    ctx.register_platform(
        name=PLATFORM_KEY,
        label="Zulip",
        adapter_factory=lambda cfg: ZulipAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY"],
        install_hint="pip install httpx",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ZULIP_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ZULIP_ALLOWED_USERS",
        allow_all_env="ZULIP_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=PLATFORM_HINT,
    )
    # M4: agent-facing tools (zulip_post, zulip_list_streams, …)
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception:
        logger.exception("[zulip] failed to register agent tools (continuing)")
