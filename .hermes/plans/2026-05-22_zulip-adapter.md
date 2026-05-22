# Hermes ↔ Zulip Adapter — Plan

**Author:** Ange (Claude / Hermes agent)
**Date:** 2026-05-22
**Workspace:** `/workspace/hermes-zulip`
**Target:** Standalone Hermes plugin (`plugins/platforms/zulip/`)
**Realm:** `https://zulip.359.wtf` (self-hosted)
**Test stream:** `#sandbox`
**Bot display name:** Ange
**Core diff:** **ZERO** — strict zero-touch plugin

> **Update after code spike:** confirmed strict zero-touch is achievable.
> `Platform._missing_` accepts `"zulip"` as a dynamic enum member, plugins ship their own
> `platform_hint` via the platform-registry entry (see `plugins/platforms/irc`, `teams`,
> `line`, `google_chat`, `simplex`), and `BasePlatformAdapter.edit_message` is a real
> base method — Hermes's stream consumer will use it for streaming responses on Zulip
> automatically the moment we implement it.

---

## 1. Goal

Build a standalone Hermes gateway extension that lets Tamas use **Zulip** as a primary
chat surface, so that:

- One Zulip **topic** (thread) = one Hermes **session** (own context, own todo list, own memory window).
- A Zulip **stream** (channel) = a Hermes **project** (groups related threads).
- Hermes can read messages, post messages, react with emoji, attach images, and **spawn a new topic**
  in a stream when it decides the current discussion has drifted to a new subject.
- Tamas can have a "future plans" topic and a "current bug" topic open side-by-side under the same
  stream, with Hermes context-switching between them naturally.

This is high-value and well-bounded: Zulip's API is one of the cleanest in the industry,
and Hermes already models threaded sessions, so most of the work is glue + a clean adapter.

---

## 2. Why it fits Hermes perfectly

Hermes' `SessionSource` (`gateway/session.py`) already has:

```python
chat_id: str                # → Zulip stream id (or stream name)
thread_id: Optional[str]    # → Zulip topic name
parent_chat_id: Optional[str]  # → stream id when thread_id is set
chat_topic: Optional[str]   # → stream description (shown to the agent in the system prompt)
```

And session keying already takes `thread_id` into account (line 595, 619 of session.py — DMs
and channels both differentiate sessions by thread_id). **So "one topic = one session" comes
for free** the moment we populate `thread_id` correctly on every inbound event.

The same pattern is used by:
- Telegram forum topics (`message_thread_id`)
- Discord threads
- Matrix threaded replies
- Slack threads (`thread_ts`)

Zulip is just a cleaner version of the same model — and unlike those platforms, *every*
message in a stream is in a topic, so the model is 100% consistent.

---

## 3. Mapping table

| Zulip concept              | Hermes concept                             | Notes |
|----------------------------|--------------------------------------------|-------|
| Realm (e.g. `359.zulipchat.com`) | Platform instance                    | One adapter, one realm |
| Stream (channel)           | `chat_id` = `stream:<id>` or `stream:<name>` | Project-level grouping |
| Topic (thread within stream) | `thread_id` = topic name                 | **The unit of a session** |
| Private message (DM)       | `chat_id` = `dm:<sorted_user_ids>`         | No topic, single-thread |
| Group PM                   | `chat_id` = `dm:<sorted_user_ids>`         | Same |
| Message                    | `MessageEvent`                             | id, sender, content, timestamp |
| Reaction (emoji)           | New adapter method `send_reaction`         | Maps to `/messages/{id}/reactions` |
| File upload                | `send_image` / `send_document`             | Two-step: upload then post markdown link |
| Bot user                   | Adapter identity (self-filter)             | Get via `/users/me` at connect |
| `users/me/subscriptions`   | List of streams the bot can hear           | Used for auth / channel directory |

---

## 4. Architecture — Plugin Path (recommended)

Per `gateway/platforms/ADDING_A_PLATFORM.md`, the **plugin path** is the right choice here:
zero changes to Hermes core, fully reloadable, can live in `~/.hermes/plugins/zulip/` or
ship as a separate repo.

Final layout:

```
/workspace/hermes-zulip/
├── README.md
├── pyproject.toml                       # optional — installable as a package
├── plugin.yaml                          # plugin manifest (env vars, requirements)
├── adapter.py                           # ZulipAdapter(BasePlatformAdapter)
├── client.py                            # thin wrapper over zulip-python SDK
├── events.py                            # event loop (queue_id + long-poll register/event)
├── identity.py                          # bot user info + self-filter
├── media.py                             # upload + markdown link rendering
├── reactions.py                         # emoji send/remove + tool exposure
├── topics.py                            # topic helpers + "spawn new topic" logic
├── standalone_sender.py                 # out-of-process delivery for cron jobs
├── system_prompt_hint.md                # injected via PLATFORM_HINTS plugin hook
└── tests/
    ├── conftest.py                      # docker-zulip fixture (optional)
    ├── test_adapter.py
    ├── test_events.py
    ├── test_topics.py
    └── test_reactions.py
```

---

## 5. plugin.yaml (manifest)

```yaml
name: zulip
version: 0.1.0
description: Zulip messaging platform with stream+topic threading
type: platform

entry_point: adapter:register

requires_env:
  - name: ZULIP_SITE
    description: Realm URL (e.g. https://359.zulipchat.com)
  - name: ZULIP_EMAIL
    description: Bot email (e.g. hermes-bot@359.zulipchat.com)
  - name: ZULIP_API_KEY
    description: Bot API key from Settings → Bots
    password: true

optional_env:
  - name: ZULIP_HOME_CHANNEL
    description: 'Default stream:topic for cron delivery (e.g. "general:announcements")'
  - name: ZULIP_ALLOWED_USERS
    description: Comma-separated list of allowed sender emails
  - name: ZULIP_ALLOW_ALL_USERS
    description: Set "true" to disable allowlist (use with caution)
  - name: ZULIP_AUTO_CREATE_TOPICS
    description: 'Set "true" to allow agent to spin up new topics on its own (default true)'

requirements:
  - zulip>=0.9.0     # official zulip-python SDK; wraps event queue API cleanly

hooks:
  env_enablement_fn: adapter:env_enablement
  apply_yaml_config_fn: adapter:apply_yaml_config
  cron_deliver_env_var: ZULIP_HOME_CHANNEL
  standalone_sender_fn: standalone_sender:send_one
```

---

## 6. Adapter skeleton (`adapter.py`)

```python
from gateway.platforms.base import (
    BasePlatformAdapter, MessageEvent, MessageType, SendResult, Platform,
)

class ZulipAdapter(BasePlatformAdapter):
    PLATFORM_KEY = "zulip"
    MAX_MESSAGE_LENGTH = 10000  # Zulip's actual limit is 10k chars

    def __init__(self, config):
        super().__init__(config, Platform.ZULIP)  # added as a string-keyed extension platform
        self.site = config.extra["site"]
        self.email = config.extra["email"]
        self.api_key = config.extra["api_key"]
        self.client = None              # zulip.Client
        self.me = None                  # /users/me
        self._event_task = None
        self._queue_id = None
        self._last_event_id = -1

    async def connect(self) -> bool:
        # 1. instantiate zulip.Client (sync SDK, wrap in run_in_executor)
        # 2. fetch /users/me → self.me (so we can self-filter)
        # 3. fetch /users/me/subscriptions (cache stream id↔name)
        # 4. register event queue for events=["message","reaction","subscription"]
        # 5. spawn self._event_task = asyncio.create_task(self._event_loop())
        ...

    async def disconnect(self):
        # cancel _event_task, /events/{queue_id} DELETE
        ...

    async def send(self, chat_id, text, *, thread_id=None, **kw) -> SendResult:
        # chat_id format:
        #   "stream:<name>"  →  type=stream, to=name, topic=thread_id
        #   "dm:<email1,email2,...>"  →  type=direct, to=[emails]
        # If chat_id is stream and thread_id is None, default to "general" topic (configurable)
        ...

    async def send_typing(self, chat_id, thread_id=None):
        # POST /typing  op=start   (auto-stop after 15s on Zulip side)
        ...

    async def send_image(self, chat_id, image_url, caption=None, thread_id=None):
        # download → POST /user_uploads → got URI → send markdown
        # [image.png](/user_uploads/...)
        ...

    async def send_image_file(self, chat_id, path, caption=None, thread_id=None):
        # POST /user_uploads with the file directly
        ...

    async def send_reaction(self, chat_id, message_id, emoji_name):
        # POST /messages/{id}/reactions {"emoji_name": emoji_name}
        # New optional adapter method — also exposed as an agent tool (§ 9)
        ...

    async def get_chat_info(self, chat_id) -> dict:
        # parse chat_id, return {"name": ..., "type": "stream"|"dm", "chat_id": chat_id}
        ...

    # ---- event loop ----
    async def _event_loop(self):
        while not self._stopping:
            try:
                events = await self._poll()       # long-poll up to ~60s
                for ev in events:
                    await self._handle_event(ev)
            except Exception:
                await asyncio.sleep(_backoff())   # reconnect with jitter
                await self._reregister_queue()

    async def _handle_event(self, ev):
        if ev["type"] != "message":
            return
        m = ev["message"]
        if m["sender_email"] == self.email:
            return                                 # self-filter
        source = self.build_source(
            chat_id=self._chat_id_for(m),
            user_id=str(m["sender_id"]),
            user_name=m["sender_full_name"],
            thread_id=m.get("subject") if m["type"] == "stream" else None,
            parent_chat_id=f"stream:{m['display_recipient']}" if m["type"] == "stream" else None,
            chat_topic=self._stream_description(m.get("stream_id")),
        )
        event = MessageEvent(
            source=source,
            message_id=str(m["id"]),
            text=m["content"],
            message_type=MessageType.TEXT,
            timestamp=m["timestamp"],
            raw=m,
        )
        await self.handle_message(event)
```

---

## 7. Inbound flow — "topic = session"

1. Zulip emits a `message` event with `subject` (topic) and `display_recipient` (stream name).
2. Adapter builds `SessionSource(chat_id="stream:engineering", thread_id="auth-bug")`.
3. `gateway/session.py` uses chat_id + thread_id to look up / create the session.
4. The agent picks up that session's history and continues the conversation **for that topic only**.
5. A message in topic "future-plans" of the same stream lands in a **different** session — exactly Tamas's mental model.

No changes needed to session.py — this already works for Telegram forum topics today.

---

## 8. Outbound flow & "spawning new topics"

Two ways Hermes can post into a *new* topic:

**A. Existing tool path** (no new code needed):
Hermes already exposes `send_message(target, message)`. We register `zulip:stream:engineering:topic-name`
as a target syntax in `tools/send_message_tool.py` routing (the only core file we touch — via the
plugin's `apply_yaml_config_fn` hook we can't add this, so we either:

  - add a single line in `send_message_tool.py` *(small built-in change, otherwise pure plugin)*, **or**
  - expose a Zulip-specific tool from the plugin: `zulip_post(stream, topic, text)`.

**Recommendation:** add the Zulip-specific tool from the plugin. Cleaner, and lets us add reactions,
topic listing, and topic rename in the same toolset.

**B. "Auto-open new topic" UX (the magic part Tamas wants):**

System-prompt hint (injected via `PLATFORM_HINTS`):

> You are on Zulip. Messages are organized into **streams** (projects) and **topics** (threads).
> Each topic is its own conversation with its own context. When the user shifts to a new subject
> that doesn't fit the current topic, you may open a new topic in the same stream by calling
> `zulip_post(stream=<current>, topic=<new-name>, text=<your-reply>)`. New topics are auto-created
> by sending to them — no setup step required. Reply in the existing topic when continuing the
> current discussion. Prefer kebab-case topic names ≤ 60 chars.

That's it — Zulip auto-creates a topic the first time a message is sent to it. The "decision to
branch" is purely a prompt-engineering layer; no extra protocol work.

---

## 9. Plugin-exposed agent tools

These are registered via the plugin's tool-registration hook (similar to how the `irc` and `teams`
plugins ship custom tools):

| Tool | Args | Purpose |
|------|------|---------|
| `zulip_post` | `stream`, `topic`, `text`, `reply_to?` | Send (auto-creates topic) |
| `zulip_edit` | `message_id`, `new_text`, `new_topic?` | Edit message content and/or move topic |
| `zulip_delete` | `message_id` | Delete a message (admin-allowed bots) |
| `zulip_react` | `message_id`, `emoji_name` | Add a reaction |
| `zulip_unreact` | `message_id`, `emoji_name` | Remove a reaction |
| `zulip_list_topics` | `stream`, `limit?` | Recent topics in a stream |
| `zulip_list_streams` | — | All subscribed streams |
| `zulip_upload_image` | `path`, `stream`, `topic`, `caption?` | Upload + post in one call |
| `zulip_rename_topic` | `message_id`, `new_topic` | Move a topic (admin-capable bots only) |

Plus **`edit_message`** as a `BasePlatformAdapter` override (not a tool — Hermes's
stream consumer calls it automatically for streaming responses). Maps to
`PATCH /messages/{id}` with `{"content": <new>}`. When Hermes is streaming a long
reply, the user will see the bubble grow live in the Zulip topic. Free win.

All seven are thin wrappers over the zulip-python SDK; ~10 LOC each.

---

## 10. Authentication & setup UX

`hermes gateway setup` should walk the user through:

1. Create a **Generic bot** in Zulip Settings → Personal → Bots (or **Incoming webhook** style
   if we go that route — but bot user is what we want for receiving).
2. Copy `Email`, `API key`, paste realm URL.
3. Subscribe the bot to the streams it should listen on (Zulip's UI: stream settings → add subscriber).
4. Optional: set `ZULIP_HOME_CHANNEL=projectX:general` so cron jobs deliver somewhere.
5. Test: `hermes gateway zulip test` → sends a 👋 to the home channel.

---

## 11. Files we *do* need to touch in Hermes core

**None.** Strict zero-touch confirmed by code spike:

| Concern | Why no core edit is needed |
|---------|---------------------------|
| `Platform` enum | `Platform._missing_` in `gateway/config.py:130` dynamically registers `"zulip"` as a pseudo-member, identity-stable. Plugin adapters already rely on this. |
| Platform hint in system prompt | `PluginPlatformRegistryEntry.platform_hint` is supported — see `plugins/platforms/{irc,teams,line,google_chat,simplex}/adapter.py`. `agent/system_prompt.py:216` reads it. |
| `send_message` tool routing | The plugin manifest's `standalone_sender_fn` makes `send_message(target="zulip:...")` work via `gateway/platform_registry.py` lookup — no `tools/send_message_tool.py` edit. |
| Cron delivery | `cron_deliver_env_var: ZULIP_HOME_CHANNEL` in `plugin.yaml` covers it. |
| Setup wizard | `requires_env` / `optional_env` rich-dict entries in `plugin.yaml` auto-populate `OPTIONAL_ENV_VARS` for `hermes setup`. |
| Message editing | `BasePlatformAdapter.edit_message` is already a first-class method. We just implement it. |

**Everything ships in `/workspace/hermes-zulip/`.** Pure plugin, drop-in install:

```bash
ln -s /workspace/hermes-zulip ~/.hermes/plugins/zulip
```

---

## 12. Testing strategy

**Unit tests** (no Zulip needed):
- `chat_id` parsing/round-trip
- Event → MessageEvent mapping
- Self-filter
- Markdown image rendering
- `SessionSource` round-trip with thread_id populated

**Integration tests** (optional, gated by env):
- Spin up `zulip/docker-zulip` locally (one container, ~3 min boot).
- Provision a realm, two users (bot + tester), one stream.
- Send a message → assert session is created with right thread_id.
- Branch to a new topic → assert new session.
- Reaction round-trip.

**Manual smoke test** before declaring it done:
1. `hermes gateway zulip start`
2. From Tamas's account, message `#test-project > setup` → bot replies in same topic.
3. Bot opens a new topic `#test-project > sub-task` proactively.
4. Tamas reacts ✅ to one of the bot's messages → reaction event arrives → bot acknowledges.
5. Bot uploads a generated image into a topic.

---

## 13. Risks & open questions

- **Zulip-python SDK is synchronous.** Wrap every call in `run_in_executor`, or use `aiohttp`
  directly against the REST endpoints (the API is small enough — ~12 endpoints — that bypassing
  the SDK is realistic and gives us proper asyncio). **Recommendation: bypass the SDK, use httpx.**
  Cleaner backpressure, no thread pool exhaustion under load.
- **Event queue expiry.** Zulip queues expire after ~10 min of inactivity. Our `_event_loop`
  must handle `BAD_EVENT_QUEUE_ID` → re-register cleanly. (Pattern already used in
  Telegram's reconnect logic — port that.)
- **Rate limits.** Zulip enforces per-bot rate limits (200 req/min by default). Honor
  `Retry-After` headers; piggyback on `_http_client_limits.py`.
- **Topic moves & renames.** If a user moves the message we're replying to, our reply might
  land in the old topic. Detect `update_message` events and update session metadata if the
  topic name changed (rare; document as known caveat for v0.1).
- **DM vs group-DM keying.** Sorted user-id tuple as `chat_id` is stable across reorderings.
- **Markdown dialect.** Zulip uses its own Markdown (close to CommonMark + spoilers + `@**name**`
  mentions). The agent should be told in the prompt hint.
- **Self-hosting reachability.** Tamas's homeserver pattern (Matrix behind WireGuard) — does he
  plan to use Zulip Cloud, self-host, or both? **Open question.** Both work identically over the
  same API, but home-channel reachability from cron jobs matters.

---

## 14. Milestones / deliverable order

A reasonable build order — each milestone is shippable on its own:

1. **M1 — Bones (1 PR):** plugin.yaml + adapter skeleton + httpx client + `/users/me` + send text
   to a hardcoded stream:topic. Manual smoke test passes.
2. **M2 — Inbound (1 PR):** event queue loop + register/reconnect + MessageEvent dispatch +
   self-filter + auth allowlist. Bot can reply in the same topic.
3. **M3 — Threading semantics (1 PR):** correct `SessionSource(thread_id=topic)` population +
   `chat_topic` from stream description. Verify two topics → two sessions in the session DB.
4. **M4 — Outbound tools (1 PR):** `zulip_post`, `zulip_list_streams`, `zulip_list_topics` +
   `PLATFORM_HINTS` entry explaining branching. End-to-end: agent decides to open a new topic.
5. **M5 — Media (1 PR):** `/user_uploads` + `send_image` + `send_image_file` + image inbound parsing.
6. **M6 — Reactions (1 PR):** `zulip_react` / `zulip_unreact` + inbound reaction events.
7. **M7 — Cron delivery (1 PR):** `standalone_sender_fn` + `ZULIP_HOME_CHANNEL` parsing
   (`stream:topic` syntax).
8. **M8 — Editing & polish (1 PR):** `edit_message` adapter override (enables live streaming
   updates) + `zulip_edit` / `zulip_delete` tools + tests, docs, setup wizard,
   `hermes gateway zulip test`.

Total: ~7–10 days of focused work, depending on how much we lean on docker-zulip for integration tests.

---

## 15. Doability assessment

**Yes — and it's actually one of the cleaner adapters to write** because:

- Zulip's API is small, REST-only, no signing dances, no WebSockets to manage (just long-poll).
- Hermes' threading model already matches Zulip's — no protocol-impedance mismatch.
- The "spawn new topic" feature is mostly a prompt-engineering line and one tool call — no
  state machine, no follow-up parsing.
- The plugin path keeps the blast radius tiny: 3 lines in Hermes core, everything else isolated
  in `/workspace/hermes-zulip/`.

The hardest part is the **event-queue reconnect loop with topic-move handling** — and even that
is a well-known pattern we already have in Telegram, Signal, and Matrix adapters to crib from.

---

## 16. Next concrete step

Greenlit. First action:

```bash
cd /workspace/hermes-zulip
# M1 scaffold: plugin.yaml + adapter.py skeleton + httpx client + /users/me probe
# + send text to #sandbox > setup-test
```

**Decisions locked in:**

1. Realm: `https://zulip.359.wtf` (self-hosted)
2. Test stream/topic: `#sandbox > <whatever Ange picks per test>`
3. Bot display name: **Ange**
4. **Strict zero-touch** plugin — no Hermes core edits
5. **Message editing in scope from day one** — `edit_message` override + `zulip_edit` tool
