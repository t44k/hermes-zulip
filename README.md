# hermes-zulip

A standalone [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin
that adds a **Zulip** gateway adapter.

## Why

Zulip's **stream + topic** model is a near-perfect fit for how Hermes already
thinks about sessions:

| Zulip          | Hermes                                  |
|----------------|-----------------------------------------|
| Stream         | Project / channel (`chat_id`)           |
| **Topic**      | **Session** (`thread_id`)               |
| Direct message | Single-session DM                       |
| Reaction       | Tool call (`zulip_react`)               |
| File upload    | Inline image / attachment send          |
| Edit           | Streaming-response in-place update      |

Each topic gets its **own session** — own context, own memory window, own todo
list. You can have a "future plans" topic and a "current bug" topic open in the
same stream, and the agent context-switches naturally.

When the agent decides the conversation has drifted to a new subject, it can
**open a new topic** in the same stream by simply sending to it — Zulip
auto-creates topics on first message.

## Status

| Milestone | Description | State |
|-----------|-------------|-------|
| M1 | Plugin scaffold + send text to a stream/topic | ✅ |
| M2 | Inbound event queue + self-filter + auth | ✅ |
| M3 | Threading semantics (one topic = one session) | ✅ |
| M4 | Outbound tools (`zulip_post`, `zulip_list_streams`, etc.) | ✅ |
| M5 | Image upload + inbound media | ✅ |
| M6 | Reactions (in + out) | ✅ |
| M7 | Cron delivery / standalone sender | scaffold present |
| M8 | Message editing + delete + polish | … |

## Install

This is a Hermes plugin — drop it into the plugins dir:

```bash
ln -s /workspace/hermes-zulip ~/.hermes/plugins/zulip
pip install httpx
```

Then configure via env (or `~/.hermes/.env`):

```bash
export ZULIP_SITE=https://zulip.example.com
export ZULIP_EMAIL=ange-bot@zulip.example.com
export ZULIP_API_KEY=...
export ZULIP_HOME_CHANNEL=sandbox:announcements     # optional
```

Verify:

```bash
hermes gateway status      # should show 💬 Zulip ✓
```

## Design

The adapter is **strict zero-touch** — zero modifications to Hermes core.
It relies on three plugin-system affordances:

1. `Platform._missing_` in `gateway/config.py` dynamically registers `"zulip"`
   as a pseudo-enum-member.
2. `platform_hint=` in `register_platform()` adds streams/topics guidance
   directly to the system prompt without editing `agent/prompt_builder.py`.
3. `standalone_sender_fn=` enables out-of-process delivery (cron + `send_message`)
   without editing `tools/send_message_tool.py`.

See `.hermes/plans/2026-05-22_zulip-adapter.md` for the full design doc.

## Smoke test

```bash
ZULIP_SITE=https://zulip.example.com \
ZULIP_EMAIL=ange-bot@... \
ZULIP_API_KEY=... \
python scripts/smoke_send.py "#sandbox" "setup-test" "👋 from Ange (M1)"
```

## License

MIT
