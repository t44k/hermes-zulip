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

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Lazy imports — main Hermes modules might not be on path during plugin discovery
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
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

PLATFORM_HINT = (
    "You are on Zulip, a chat platform organised into **streams** "
    "(project-level channels) and **topics** (threaded conversations inside a stream). "
    "Each topic is its own conversation with its own context — when the user shifts "
    "to a new subject that doesn't fit the current topic, you may open a new topic "
    "in the same stream by sending a message to it (topics are auto-created on first "
    "post). Prefer short kebab-case topic names (≤ 60 chars). "
    "Zulip Markdown is supported: **bold**, *italic*, `code`, ```code blocks```, "
    "tables, spoilers (||spoiler||), and @-mentions like @**Tamas**. "
    "Reply in the existing topic when continuing the same discussion. "
    "Messages can be edited; long streamed responses will update in place."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_chat_id(chat_id: str) -> tuple[str, str]:
    """Parse a Hermes chat_id into (kind, value).

    Accepts:
      ``stream:<name>``     →  ("stream", "<name>")
      ``dm:<a@x,b@y>``      →  ("dm", "<a@x,b@y>")
      bare ``<name>``       →  ("stream", "<name>")   (lenient default)
    """
    if ":" in chat_id:
        kind, _, val = chat_id.partition(":")
        if kind in ("stream", "dm"):
            return kind, val
    return "stream", chat_id


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

        self._client: Optional[ZulipClient] = None
        self._me: dict = {}
        self._streams_by_name: dict[str, dict] = {}

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
            subs = await self._client.get_subscriptions()
            self._streams_by_name = {s["name"]: s for s in subs}
            logger.info(
                "[zulip] connected as %s (%s) — %d subscribed streams",
                self._me.get("full_name"),
                self._me.get("email"),
                len(self._streams_by_name),
            )
            return True
        except ZulipAPIError as e:
            logger.error("[zulip] connect failed: %s", e)
            return False
        except Exception:
            logger.exception("[zulip] connect crashed")
            return False

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.exception("[zulip] error during disconnect")
        self._client = None

    # ---------- outbound --------------------------------------------------

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        thread_id: Optional[str] = None,
        **_kwargs: Any,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="zulip: not connected")
        kind, val = _parse_chat_id(chat_id)
        try:
            if kind == "stream":
                topic = thread_id or DEFAULT_TOPIC
                msg_id = await self._client.send_stream_message(val, topic, text)
            else:  # dm
                recipients = [e.strip() for e in val.split(",") if e.strip()]
                msg_id = await self._client.send_direct_message(recipients, text)
            return SendResult(success=True, message_id=str(msg_id))
        except ZulipAPIError as e:
            logger.error("[zulip] send failed: %s", e)
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.exception("[zulip] send crashed")
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, thread_id: Optional[str] = None) -> None:
        # M2: implement via POST /typing. No-op for M1.
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> SendResult:
        # M5: download → /user_uploads → markdown link. For M1, just send the URL.
        body = f"{caption}\n\n{image_url}" if caption else image_url
        return await self.send(chat_id, body, thread_id=thread_id)

    # ---------- introspection --------------------------------------------

    async def get_chat_info(self, chat_id: str) -> dict:
        kind, val = _parse_chat_id(chat_id)
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
    **_kwargs: Any,
) -> dict:
    """Out-of-process send for cron jobs that don't have a live adapter."""
    extra = pconfig.extra or {}
    site = extra.get("site") or os.getenv("ZULIP_SITE", "")
    email = extra.get("email") or os.getenv("ZULIP_EMAIL", "")
    key = extra.get("api_key") or pconfig.token or os.getenv("ZULIP_API_KEY", "")
    verify_tls = _truthy(extra.get("verify_tls", os.getenv("ZULIP_VERIFY_TLS", "true")))
    if not (site and email and key):
        return {"success": False, "error": "zulip: missing credentials"}

    # chat_id formats accepted:
    #   "stream:<name>:<topic>"
    #   "stream:<name>"   (uses DEFAULT_TOPIC)
    #   "<name>:<topic>"  (lenient — assumes stream)
    #   "dm:a@x.com,b@y.com"
    parts = chat_id.split(":")
    if parts[0] == "dm" and len(parts) >= 2:
        recipients = [p.strip() for p in parts[1].split(",") if p.strip()]
        async with ZulipClient(site, email, key, verify_tls=verify_tls) as c:
            mid = await c.send_direct_message(recipients, message)
        return {"success": True, "message_id": str(mid)}
    if parts[0] == "stream":
        stream = parts[1] if len(parts) > 1 else ""
        topic = parts[2] if len(parts) > 2 else DEFAULT_TOPIC
    elif len(parts) == 2:
        stream, topic = parts[0], parts[1]
    else:
        stream, topic = parts[0], DEFAULT_TOPIC

    if not stream:
        return {"success": False, "error": "zulip: empty stream in chat_id"}

    try:
        async with ZulipClient(site, email, key, verify_tls=verify_tls) as c:
            mid = await c.send_stream_message(stream, topic, message)
        return {"success": True, "message_id": str(mid)}
    except ZulipAPIError as e:
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
