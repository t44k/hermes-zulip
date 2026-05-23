# Changelog

All notable changes to **hermes-zulip** are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com).

## [Unreleased]

### Changed

- **BREAKING (default behaviour, not API):** `ZULIP_TAG_OUTGOING_IDS`
  defaults to **`false`** (was `true`). The auto-prepended `[msg #N]`
  prefix on every outbound message was visible noise for human readers
  in the channel, while the agent can already identify its own past
  posts via sender name + `id` returned by `zulip_fetch`. Users who
  relied on the prefix can opt back in with `ZULIP_TAG_OUTGOING_IDS=true`.
  Platform-hint string updated to drop the "your outbound messages are
  auto-tagged" sentence. Regression test added so the default can't
  silently flip back.

## [0.1.0] — 2026-05-23

First releasable cut — feature-complete for a single-user Hermes ↔ Zulip
homelab bridge. 244 tests, 0 failures.

### Added

- **M1** — Plugin scaffold, `httpx`-based REST client, `Platform._missing_`
  dynamic enum registration. Sends text to `stream > topic`. (`8a83531`)
- **M2** — Long-poll event queue, message dispatch, self-filter,
  reconnection with exponential backoff + jitter. (`d99bc10`)
- **M3** — Threading semantics: one Zulip topic = one Hermes session
  via `SessionSource.thread_id`.
- **M4** — Six agent-facing tools (`zulip_post`, `zulip_dm`,
  `zulip_list_streams`, `zulip_list_topics`, `zulip_upload_image`,
  later joined by `zulip_react`, `zulip_edit`, `zulip_delete`,
  `zulip_fetch`). (`04c77e0`)
- **M5** — Image upload via `/user_uploads`, inbound media download &
  caching, auto-promote single-image messages to `MessageType.PHOTO`.
  (`432ffcb`)
- **M6** — Emoji reactions in both directions. User → bot reactions are
  surfaced as synthetic agent events. (`b0fb0a0`)
  - **Polish** — `:eyes:` "seen / done" lifecycle reaction. Added the
    moment a message arrives, removed when the turn completes, left
    in place on crash as a clear signal. Configurable via
    `ZULIP_AUTO_SEEN_REACTION` and `ZULIP_SEEN_EMOJI`. (`eb07fe9`)
- **M7** — Standalone sender for cron / out-of-process `send_message`.
  Honours `thread_id` kwarg, uploads `media_files`, applies the
  `[msg #N]` auto-tag. (`2b54820`)
- **M8** — `zulip_edit` and `zulip_delete` tools. Inbound
  `update_message` events surface as synthetic agent events (with
  self-edit filter to avoid feedback loops). (`e71e916`)
- **M9** — Outbound `[msg #N]` auto-tag prefix on every send, plus
  `zulip_fetch` tool wrapping `GET /api/v1/messages` for history reads.
  Behind `ZULIP_TAG_OUTGOING_IDS` flag (default `true`). (`a40aa78`)
- **M10** — Streaming-response in-place edits via `adapter.edit_message`.
  Re-applies the `[msg #N]` prefix on every edit. (`82dc0e2`)
- **M11** — Test counter accounting fixed; env-contamination
  test stripped of `ZULIP_*` env vars. GitHub Actions CI matrix on
  Python 3.11 + 3.12. (`962e4b5`)
- **M12** — `ZULIP_TRIGGER_MODE` config: `all` (default) replies to every
  message, `mention_only` requires `@`-mention or DM. Detects mentions
  via Zulip event flags (`mentioned`, `wildcard_mentioned`,
  `topic_wildcard_mentioned`, `stream_wildcard_mentioned`) with a
  content-scan fallback for older Zulip versions. (`79bf3a1`)

### Fixed

- Adapter `send()` accepting both `(chat_id, content, metadata)` and
  legacy `(chat_id, text=, thread_id=)` keyword forms. (`ee96db3`)
- Tool-handler signature: `(args: dict, **kwargs)` instead of unpacked
  positionals — the dispatcher injects `task_id` as a kwarg. (`8da46a1`,
  `9864226`)
- Inbound-message `[msg #<id>]` prefix so the agent can target real IDs
  instead of guessing. (`0b61347`)
- Cron-style 3-segment chat_ids (`stream:foo:topic`) routed correctly
  in the live adapter's `send()` — previously got `(no topic)` because
  `_parse_chat_id` only split on the first colon. (`27b83d3`)
