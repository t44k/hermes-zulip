# hermes-zulip

[![tests](https://github.com/t44k/hermes-zulip/actions/workflows/tests.yml/badge.svg)](https://github.com/t44k/hermes-zulip/actions/workflows/tests.yml)
[![Hermes Agent](https://img.shields.io/badge/Hermes%20Agent-plugin-blueviolet)](https://github.com/NousResearch/hermes-agent)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A standalone [Hermes Agent](https://github.com/NousResearch/hermes-agent)
plugin that adds a **Zulip** gateway adapter — zero-touch (no upstream
patches), batteries included.

---

## Why Zulip?

Zulip's **stream + topic** model maps cleanly onto how Hermes already
thinks about sessions:

| Zulip          | Hermes                                    |
|----------------|-------------------------------------------|
| Stream         | Project / channel (`chat_id`)             |
| **Topic**      | **Session** (`thread_id`) — own context   |
| Direct message | Single-session DM                         |
| Reaction       | Tool call (`zulip_react`) + seen lifecycle |
| File upload    | Inline image / attachment send            |
| Edit           | Streaming-response in-place update        |

Each Zulip topic becomes its own Hermes session — own context window, own
memory, own todo list. Park a "future plans" topic and a "current bug"
topic in the same stream and the agent context-switches naturally between
them.

When the conversation drifts to a new subject, the agent can **open a
new topic** in the same stream just by sending to it — Zulip
auto-creates topics on first message.

---

## Feature matrix

| Milestone | Description | State |
|-----------|-------------|-------|
| M1  | Plugin scaffold + text send to stream/topic | ✅ |
| M2  | Inbound event queue + self-filter + auth   | ✅ |
| M3  | Threading (one topic = one session)        | ✅ |
| M4  | Agent tools (`zulip_post` / `dm` / `list_streams` / `list_topics`) | ✅ |
| M5  | Image upload + inbound media               | ✅ |
| M6  | Reactions in + out + `:eyes:` seen/done lifecycle | ✅ |
| M7  | Cron / standalone sender (thread_id + media + auto-tag) | ✅ |
| M8  | `zulip_edit` + `zulip_delete` + inbound edit events | ✅ |
| M9  | `zulip_fetch` history + `[msg #N]` auto-tag | ✅ |
| M10 | Streaming responses (in-place edits)       | ✅ |
| M11 | Test cleanup + GitHub Actions CI           | ✅ |
| M12 | Configurable trigger mode (`all` / `mention_only`) | ✅ |

**244 tests, 0 failures** across all milestones.

---

## Install

### 1. Drop the plugin into Hermes

```bash
git clone git@github.com:t44k/hermes-zulip.git
ln -s "$PWD/hermes-zulip" ~/.hermes/plugins/zulip
pip install httpx
```

### 2. Create a Zulip bot

In Zulip: **Settings → Personal → Bots → Add bot →
Generic bot**. Subscribe it to the streams it should listen on.

### 3. Configure

Add the credentials to `~/.hermes/.env`:

```bash
ZULIP_SITE=https://zulip.example.com
ZULIP_EMAIL=ange-bot@zulip.example.com
ZULIP_API_KEY=...secret...
```

### 4. Enable the plugin

```bash
hermes plugins enable zulip-platform
hermes gateway status      # should show 💬 Zulip ✓
```

### 5. (Optional) Turn on streaming responses

In `~/.hermes/config.yaml`:

```yaml
streaming:
  enabled: true
```

Restart the gateway. Ange's long replies will now stream in token-by-token.

---

## Configuration reference

All settings can be supplied as either an `extra:` field in
`~/.hermes/config.yaml` (under `platforms.zulip.extra`) or as the
matching `ZULIP_*` env var.

### Required

| Env / Config key   | Description                                                    |
|--------------------|----------------------------------------------------------------|
| `ZULIP_SITE`       | Realm URL, e.g. `https://zulip.example.com`                    |
| `ZULIP_EMAIL`      | Bot user email (from **Settings → Bots**)                      |
| `ZULIP_API_KEY`    | API key shown next to the bot                                  |

### Common knobs

| Env / Config key            | Default     | Effect |
|-----------------------------|-------------|--------|
| `ZULIP_HOME_CHANNEL`        | *unset*     | Default destination for cron / `send_message`. Format `stream:topic` (e.g. `sandbox:announcements`). |
| `ZULIP_ALLOWED_USERS`       | *unset*     | Comma-separated emails or @-mention names allowed to talk to the bot. |
| `ZULIP_ALLOW_ALL_USERS`     | `false`     | Skip the allowlist and accept anyone in subscribed streams (dev only). |
| `ZULIP_VERIFY_TLS`          | `true`      | Set `false` for self-signed dev realms. |
| `ZULIP_AUTO_CREATE_TOPICS`  | `true`      | Whether the agent may open new topics on subject changes. |

### Behavioural switches

| Env / Config key            | Default     | Effect |
|-----------------------------|-------------|--------|
| `ZULIP_TRIGGER_MODE`        | `all`       | `all` = reply to every non-self message in subscribed streams. `mention_only` = only stream messages that `@`-mention the bot (or DMs). |
| `ZULIP_AUTO_SEEN_REACTION`  | `true`      | Add `:eyes:` to every inbound message, remove on successful turn completion. Leave in place on error/crash. |
| `ZULIP_SEEN_EMOJI`          | `eyes`      | Short-name for the seen-reaction. Try `mag`, `see_no_evil`, or `sparkles`. |
| `ZULIP_TAG_OUTGOING_IDS`    | `false`     | If `true`, auto-prepend `[msg #N] ` to every outbound message via a follow-up PATCH so the bot can identify its own posts when scrolling history. Off by default — visible in-channel noise outweighs the convenience, since `zulip_fetch` already returns sender + id per message. |

---

## Agent-facing tools

The plugin registers **9 tools** in the `hermes-zulip` toolset. The agent
gets these automatically; you don't have to wire anything up.

| Tool                | Purpose                                                         |
|---------------------|-----------------------------------------------------------------|
| `zulip_post`        | Post to `stream > topic`. Spawns a new topic on first message. |
| `zulip_dm`          | Send a direct message to one or more users by email.            |
| `zulip_list_streams`| List streams the bot is subscribed to.                          |
| `zulip_list_topics` | List recent topics inside a stream.                             |
| `zulip_upload_image`| Upload a local image file + post it inline.                     |
| `zulip_react`       | Add or remove an emoji reaction on a message.                   |
| `zulip_edit`        | Edit one of the bot's own messages (fix typo, retract, etc.).   |
| `zulip_delete`      | Delete one of the bot's own messages.                           |
| `zulip_fetch`       | Read recent history from a topic / DM (for summaries, quoting). |

The system prompt explains to the agent when each tool fits via the
`platform_hint` in `adapter.py`.

---

## Architecture

The adapter is **strict zero-touch** — zero modifications to Hermes core.
Three plugin-system affordances make this possible:

1. **`Platform._missing_`** in `gateway/config.py` dynamically registers
   `"zulip"` as a pseudo-enum-member.
2. **`platform_hint=`** in `register_platform()` injects the
   stream/topic guidance into the system prompt without editing
   `agent/prompt_builder.py`.
3. **`standalone_sender_fn=`** enables out-of-process delivery (cron +
   `send_message` from subagents) without editing
   `tools/send_message_tool.py`.

### File layout

```
hermes-zulip/
├── __init__.py            # entry point — exports `register`
├── adapter.py             # ZulipAdapter (event loop, send, edit, reactions, …)
├── client.py              # Thin httpx wrapper around Zulip REST
├── tools.py               # 9 agent-facing tools
├── plugin.yaml            # Plugin metadata + env declarations
├── pyproject.toml         # Package metadata
├── scripts/               # Smoke tests for live verification
│   ├── smoke_send.py
│   ├── smoke_listen.py
│   └── smoke_tools.py
├── tests/run_tests.py     # 244 unit tests — runs without pytest
├── .github/workflows/     # CI: matrix on Python 3.11 + 3.12
└── .hermes/plans/         # Full design doc
```

### Streaming-response flow

When `streaming.enabled: true` and the agent produces a long reply, the
gateway's `GatewayStreamConsumer`:

1. Calls `adapter.send()` for the first chunk → grabs the new
   `message_id`.
2. For each subsequent chunk, calls `adapter.edit_message(chat_id,
   message_id, content, *, finalize=False)`, which our adapter routes
   through `client.update_message` (PATCH `/api/v1/messages/{id}`).
3. The bot's auto-`[msg #N]` prefix is re-applied on every edit so the
   ID stays visible.
4. On the final chunk, `finalize=True` is passed; the consumer skips
   the trailing cursor.

Self-edit events from these PATCHes are filtered upstream in
`_handle_update_message_event` (by `user_id == bot_id`), so the agent
doesn't see its own streaming edits as user-edits coming back in.

---

## Smoke tests

The `scripts/` directory has three standalone scripts for live testing
against a real realm:

```bash
# 1. Send a single text message
ZULIP_SITE=https://zulip.example.com \
ZULIP_EMAIL=ange-bot@... \
ZULIP_API_KEY=... \
python scripts/smoke_send.py sandbox setup-test "👋 hello from Ange"

# 2. Listen for inbound events (Ctrl-C to stop)
python scripts/smoke_listen.py

# 3. Exercise every agent tool once
python scripts/smoke_tools.py
```

## Unit tests

Self-contained, no `pytest` required:

```bash
PYTHONPATH=/path/to/hermes-agent python tests/run_tests.py
```

Expected output:

```
M1  results: 36 passed, 0 failed
M2  results: 21 passed, 0 failed
M4  results: 43 passed, 0 failed
M5  results: 22 passed, 0 failed
M6  results: 20 passed, 0 failed
M6 polish:   9 passed, 0 failed
M7  results: 16 passed, 0 failed
M8  results: 26 passed, 0 failed
M9  results: 19 passed, 0 failed
M10 results: 13 passed, 0 failed
M12 results: 19 passed, 0 failed
```

CI runs the suite on every push and PR on Python 3.11 + 3.12 — see
[`.github/workflows/tests.yml`](.github/workflows/tests.yml).

---

## Troubleshooting

**Bot doesn't reply to messages.**
Check `hermes gateway status` — Zulip should show ✓. If it does, check
`~/.hermes/logs/gateway.log` for `[zulip]` lines. If you see
`mention_only: skipping unaddressed msg`, you've set
`ZULIP_TRIGGER_MODE=mention_only` and need to `@`-mention the bot or DM
it.

**"Channel does not exist" when posting from cron.**
Make sure your `--deliver` flag uses the format
`zulip:stream:<name>:<topic>` (4 segments). Three-segment chat_ids like
`stream:name:topic` are supported, but `--deliver zulip:stream` falls
back to `(no topic)`.

**Streaming responses arrive as one big message.**
Verify `streaming.enabled: true` in `~/.hermes/config.yaml` (NOT
`gateway.yaml` — that file isn't loaded). Then `hermes gateway
restart`.

**`:eyes:` reaction never disappears.**
The bot crashed mid-turn. Check `~/.hermes/logs/errors.log`. The
lingering eye is intentional — it's a signal that the turn didn't
complete. Set `ZULIP_AUTO_SEEN_REACTION=false` to disable.

**Outbound messages have `[msg #N]` prefix and you don't want it.**
That feature is **off by default** as of v0.2 — if you're seeing the prefix
you either set `ZULIP_TAG_OUTGOING_IDS=true` somewhere or upgraded from an
earlier build. Unset the env var (or set it to `false`) and restart the
gateway. The agent identifies its own past posts via sender name + `id`
returned by `zulip_fetch`.

---

## Design doc

The full multi-stage design plan lives at
[`.hermes/plans/2026-05-22_zulip-adapter.md`](.hermes/plans/2026-05-22_zulip-adapter.md)
— architecture decisions, milestone breakdown, and the strict
zero-touch contract.

---

## License

[MIT](LICENSE). Built by Tamas and Ange (Claude).
