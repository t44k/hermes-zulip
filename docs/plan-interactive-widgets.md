# Plan — Interactive widgets (buttons / forms) for the Zulip adapter

**Status:** draft, 2026-05-25
**Author:** Ange (for Tamas)
**Scope:** add ability for the agent to send Zulip messages that render as
**clickable buttons** in the Zulip web/desktop app, and to receive the button
press as a regular inbound message that the agent can react to.

---

## 1. What Zulip actually supports

Zulip has two distinct "interactive message" mechanisms — both undocumented in
the public REST API reference, both implemented under `zerver/lib/widget.py` +
`web/src/widgetize.ts` in the Zulip server source. Source: the [Widgets dev
docs](https://zulip.readthedocs.io/en/stable/subsystems/widgets.html).

### a) **Slash widgets** — `/poll`, `/todo`

- Sent as a normal message whose `content` starts with `/poll <question>` or
  `/todo <title>`.
- Server detects the prefix and attaches a `SubMessage` row of type `poll` /
  `todo`. Web client renders an interactive poll / checklist; other clients
  fall back to plain text.
- **Pros:** zero schema knowledge, just send `/poll Tea or coffee?\nA\nB\nC`.
- **Cons:** the options are not button-driven action-replies — clicks update
  poll state via `/json/submessage`, not as new chat messages. The bot would
  need to subscribe to `submessage` events to learn who voted what.

### b) **zform** — generic button form (this is what we want)

- Sent by **including a `widget_content` field** alongside `content` on
  `POST /api/v1/messages`.
- The payload's `extra_data.choices` defines an array of buttons. Clicking a
  button **sends a brand-new Zulip message from the clicking user** whose
  content equals the `reply` field on that choice.
- That means: **we already handle button clicks**. They arrive at our adapter
  as ordinary inbound messages, route through `_handle_message_event`, and the
  agent sees them in its turn.
- The fallback for non-web clients is whatever you put in `content` — usually a
  textual rendering of the question and the choices.
- Web/desktop only; mobile and terminal apps render fallback text. (Tamas uses
  Zulip web + desktop, so this is fine.)

**Example payload (verified against the dev docs):**

```json
POST /api/v1/messages
{
  "type": "stream",
  "to": "sandbox",
  "topic": "approvals",
  "content": "Approve deployment of v0.3?  (A) yes  (B) no  (C) needs review",
  "widget_content": {
    "widget_type": "zform",
    "extra_data": {
      "type": "choices",
      "heading": "Approve deployment of v0.3?",
      "choices": [
        {"type": "multiple_choice", "short_name": "A", "long_name": "yes",          "reply": "/approval v0.3 yes"},
        {"type": "multiple_choice", "short_name": "B", "long_name": "no",           "reply": "/approval v0.3 no"},
        {"type": "multiple_choice", "short_name": "C", "long_name": "needs review", "reply": "/approval v0.3 review"}
      ]
    }
  }
}
```

Important detail: `widget_content` must be **JSON-encoded as a string** in the
form body when using `POST /api/v1/messages` with `application/x-www-form-urlencoded`
(which is what `aiohttp` + our `_request` helper already does for `data=`). I.e.
we serialize it with `json.dumps(...)` on the way out.

---

## 2. Design — Hermes-Zulip side

Three pieces, all additive, no breaking changes:

### M13a — extend `client.py`

Add an optional `widget_content: dict | None` arg to `send_stream_message` and
`send_direct_message`. When present, `json.dumps` it and include it as a form
field. No new endpoint, no new code path for the response (Zulip returns the
same `{id: …}` shape).

### M13b — extend `tools.py`

Add a new agent tool, **`zulip_buttons`**, with this schema:

```yaml
zulip_buttons:
  description: Send a Zulip message with clickable reply buttons (zform).
    Clicking a button sends the configured "reply" text back as a new message
    from the clicker. Works in Zulip web + desktop; mobile shows the fallback
    text only.
  parameters:
    stream: optional — required if dm_to is omitted
    topic:  optional — required for stream messages
    dm_to:  optional list[str] — emails for a DM instead of a stream post
    heading: short question shown above the buttons
    choices: list of {label: str, reply: str}, max ~10
    fallback_text: optional — what non-web clients see (default: auto-built
      from heading + labels)
  returns: {message_id: int}
```

This is a thin wrapper that builds the `widget_content` dict and calls the
extended `send_stream_message` / `send_direct_message`.

We do **not** add a separate `zulip_poll` tool yet — `/poll` is easier (just
send `/poll Question\nA\nB`) and the agent can already do that through
`zulip_post`. We can add a dedicated tool later if polling state-tracking
becomes a real need (see M13d below).

### M13c — inbound handling: nothing changes

Button clicks arrive as ordinary `message` events with `content` equal to the
choice's `reply` string. The existing `_handle_message_event` in `adapter.py`
already routes them. The only convention we adopt:

- **Prefix replies with a stable token** the agent emits (e.g. `/approval`,
  `/confirm`, `/cancel`) so the agent can pattern-match its own outstanding
  forms against incoming replies without ambiguity.
- The clicker's user is the message's `sender_email` / `sender_id` — already
  surfaced.

Optional polish: in `_handle_message_event`, if the inbound message looks like
a zform-reply (configurable prefix list, e.g. `["/approval", "/confirm", "/cancel"]`),
auto-add a `[zform-reply from <sender>]` tag to the prefix string. **Skip for
v1** — it's noise; the agent can read the content directly.

### M13d — (deferred) submessage events for `/poll` and `/todo`

If we later want the agent to observe poll votes in real time without users
explicitly typing a reply, register the event queue for `submessage` events
in addition to `message`, and surface them as a new inbound event type. Out of
scope for this iteration.

---

## 3. Tests / verification

1. Unit: `tests/run_tests.py` — add a fake-HTTP test that calls
   `zulip_buttons` and asserts the outgoing POST body contains
   `widget_content=<json-string>` and the JSON parses to the expected shape.
2. Smoke: extend `scripts/smoke_send.py` (or add `scripts/smoke_widgets.py`)
   that posts a 3-button zform to `#sandbox > widget-test` against the
   live `zulip.359.wtf` and prints the message id.
3. End-to-end: Tamas clicks a button in the web app; verify the inbound
   message arrives at the adapter with `content == reply` and routes to the
   agent's session.
4. Fallback: open the same message in the mobile app, verify the textual
   fallback is readable.

---

## 4. Open questions for Tamas

1. **Confirmation pattern.** Are you happy with the "button click sends a
   slash-prefixed message that the agent then sees" model? It's clean,
   audit-friendly (the reply is visible to everyone in the topic), and matches
   how Zulip's own trivia bot works. Alternative: silent submessage events,
   but then the conversation transcript loses the audit trail.
2. **Tool name.** `zulip_buttons` vs `zulip_form` vs `zulip_choices`?
   `zulip_buttons` is plain English but technically these are choice buttons
   only — no free-text inputs, date pickers, etc. (Zulip's zform schema
   doesn't support those; it's choices-only.)
3. **Default trigger behaviour for click-replies.** With `trigger_mode=mention_only`,
   a button click currently won't wake the agent unless the `reply` text
   mentions the bot. Options:
   (a) prefix every reply with `@**Ange**` automatically when building
       widget_content (visible, ugly);
   (b) special-case messages whose content starts with a registered slash
       token (e.g. `/approval`) in `_handle_message_event` so they bypass the
       mention gate;
   (c) document that users running in mention_only mode must include the bot
       handle in the reply string they configure.
   My recommendation: **(b)**, gated by a configurable allow-list
   (`zulip.widget_reply_prefixes`, default `["/approval", "/confirm", "/cancel"]`).
