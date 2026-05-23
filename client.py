"""Thin httpx wrapper around the Zulip REST API.

We deliberately bypass the synchronous ``zulip`` Python SDK and talk to the
REST surface directly: the API is small, all endpoints we care about are
straightforward form-urlencoded or multipart, and httpx gives us proper
asyncio + per-request timeout control + connection pooling.

Auth is HTTP Basic with (email, api_key).

Endpoints used in M1:
  * GET    /api/v1/users/me                   — bot identity probe
  * GET    /api/v1/users/me/subscriptions     — list streams the bot is in
  * POST   /api/v1/messages                   — send a message

Later milestones add:
  * POST   /api/v1/register                   — open event queue
  * GET    /api/v1/events                     — long-poll
  * PATCH  /api/v1/messages/{id}              — edit
  * DELETE /api/v1/messages/{id}              — delete
  * POST   /api/v1/messages/{id}/reactions    — emoji react
  * POST   /api/v1/user_uploads               — file upload
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class ZulipAPIError(Exception):
    """A non-success response from the Zulip server."""

    def __init__(self, status: int, code: str, msg: str, raw: dict | None = None):
        super().__init__(f"Zulip API error {status}/{code}: {msg}")
        self.status = status
        self.code = code
        self.msg = msg
        self.raw = raw or {}


class ZulipClient:
    """Minimal async Zulip REST client.

    Usage::

        async with ZulipClient(site, email, api_key) as c:
            me = await c.get_me()
            await c.send_stream_message("sandbox", "setup-test", "hello")
    """

    def __init__(
        self,
        site: str,
        email: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30.0,
    ):
        # Normalise: strip trailing slash, ensure scheme.
        site = site.rstrip("/")
        if not site.startswith(("http://", "https://")):
            site = "https://" + site
        self.site = site
        self.email = email
        self.api_key = api_key
        self.base_url = f"{site}/api/v1"
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # ---- lifecycle ----------------------------------------------------

    async def __aenter__(self) -> "ZulipClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=(self.email, self.api_key),
                verify=self._verify_tls,
                timeout=self._timeout,
                headers={"User-Agent": "hermes-zulip/0.1.0"},
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- core request -------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict:
        assert self._client is not None, "ZulipClient not connected (call .connect() or use async with)"

        # Zulip expects list/dict fields JSON-encoded as form values.
        if data is not None:
            import json as _json
            data = {
                k: (_json.dumps(v) if isinstance(v, (list, dict)) else v)
                for k, v in data.items()
                if v is not None
            }
        if params is not None:
            params = {k: v for k, v in params.items() if v is not None}

        resp = await self._client.request(
            method,
            path,
            params=params,
            data=data,
            files=files,
            json=json_body,
            timeout=timeout or self._timeout,
        )

        # Zulip returns JSON for both success and error responses.
        try:
            payload = resp.json()
        except Exception:
            raise ZulipAPIError(resp.status_code, "non_json", resp.text[:500])

        if payload.get("result") != "success":
            raise ZulipAPIError(
                resp.status_code,
                payload.get("code", "unknown"),
                payload.get("msg", "(no message)"),
                raw=payload,
            )

        return payload

    # ---- M1 endpoints -------------------------------------------------

    async def get_me(self) -> dict:
        """Fetch the bot's own user record (used for self-filter + display)."""
        return await self._request("GET", "/users/me")

    async def get_subscriptions(self) -> list[dict]:
        """List streams the bot is subscribed to."""
        resp = await self._request("GET", "/users/me/subscriptions")
        return resp.get("subscriptions", [])

    async def send_stream_message(
        self,
        stream: str,
        topic: str,
        content: str,
    ) -> int:
        """Send a message to ``stream > topic``. Returns the new message id."""
        resp = await self._request(
            "POST",
            "/messages",
            data={
                "type": "stream",
                "to": stream,
                "topic": topic,
                "content": content,
            },
        )
        return int(resp["id"])

    async def send_direct_message(
        self,
        recipient_emails: list[str],
        content: str,
    ) -> int:
        """Send a 1:1 or group DM."""
        resp = await self._request(
            "POST",
            "/messages",
            data={
                "type": "direct",
                "to": recipient_emails,
                "content": content,
            },
        )
        return int(resp["id"])

    # ---- M2: event queue endpoints ------------------------------------

    async def register_event_queue(
        self,
        event_types: list[str] | None = None,
        narrow: list[list[str]] | None = None,
        *,
        all_public_streams: bool = False,
    ) -> dict:
        """Open a long-poll event queue.

        Returns the full register response (queue_id, last_event_id, plus
        whatever bootstrap state was requested by ``fetch_event_types`` —
        we omit that field so the response is light).
        """
        data: dict[str, Any] = {
            "event_types": event_types or ["message"],
            "all_public_streams": all_public_streams,
        }
        if narrow is not None:
            data["narrow"] = narrow
        return await self._request("POST", "/register", data=data)

    async def get_events(
        self,
        queue_id: str,
        last_event_id: int,
        *,
        dont_block: bool = False,
        timeout: float = 70.0,
    ) -> list[dict]:
        """Long-poll for new events.

        Default ``timeout`` is 70s — Zulip itself caps long-polls at ~60s,
        so we give the server a small margin to respond with a heartbeat
        before our HTTP client times out.
        """
        resp = await self._request(
            "GET",
            "/events",
            params={
                "queue_id": queue_id,
                "last_event_id": last_event_id,
                "dont_block": "true" if dont_block else "false",
            },
            timeout=timeout,
        )
        return resp.get("events", [])

    async def delete_event_queue(self, queue_id: str) -> None:
        """Close an event queue (best-effort — ignore errors on shutdown)."""
        try:
            await self._request(
                "DELETE", "/events", params={"queue_id": queue_id}, timeout=10.0,
            )
        except Exception:
            logger.debug("[zulip] delete_event_queue failed (ignored)", exc_info=True)

    # ---- M5: media endpoints ------------------------------------------

    async def upload_file(
        self,
        filename: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> str:
        """Upload a file to Zulip. Returns the ``/user_uploads/...`` uri.

        The returned uri is realm-relative (e.g. ``/user_uploads/2/ab/cd/foo.png``);
        prefix with ``self.site`` to get an absolute URL for download.
        """
        resp = await self._request(
            "POST",
            "/user_uploads",
            files={"file": (filename, data, mime)},
        )
        uri = resp.get("uri") or resp.get("url")
        if not uri:
            raise ZulipAPIError(0, "upload_no_uri", "upload returned no uri", raw=resp)
        return uri

    async def download_user_upload(self, uri: str) -> bytes:
        """Download an authenticated Zulip attachment.

        ``uri`` may be a full URL (``https://realm/user_uploads/...``) or the
        realm-relative path (``/user_uploads/...``) as returned by the upload
        endpoint and embedded in message markdown. Returns the raw bytes.
        """
        assert self._client is not None, "ZulipClient not connected"
        # Normalise to an absolute URL against the realm root (not /api/v1).
        if uri.startswith("http://") or uri.startswith("https://"):
            url = uri
        else:
            if not uri.startswith("/"):
                uri = "/" + uri
            url = self.site + uri
        resp = await self._client.get(url, timeout=self._timeout, follow_redirects=True)
        if resp.status_code != 200:
            raise ZulipAPIError(
                resp.status_code, "download_failed",
                f"GET {url} returned {resp.status_code}",
            )
        return resp.content
