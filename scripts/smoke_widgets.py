#!/usr/bin/env python3
"""Live smoke: post a 3-button zform widget to #sandbox > widget-test.

Usage:
    PYTHONPATH=/home/devuser/.hermes/hermes-agent \\
      /home/devuser/.hermes/hermes-agent/venv/bin/python3 scripts/smoke_widgets.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make the plugin importable as a package
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))  # so "hermes_zulip"-style imports could resolve

# Direct-import client.py without the relative-import dance
import importlib.util
spec = importlib.util.spec_from_file_location("zulip_client_smoke", ROOT / "client.py")
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)
ZulipClient = mod.ZulipClient
ZulipAPIError = mod.ZulipAPIError


STREAM = os.environ.get("SMOKE_STREAM", "sandbox")
TOPIC = os.environ.get("SMOKE_TOPIC", "widget-test")
HEADING = "Smoke test: which is your favourite Zulip widget?"
CHOICES = [
    {"label": "polls",     "reply": "/approval widget poll"},
    {"label": "todo list", "reply": "/approval widget todo"},
    {"label": "zform",     "reply": "/approval widget zform"},
]


def _build_zform(heading, choices):
    letters = "ABCDEFGHIJ"
    out = []
    for i, c in enumerate(choices):
        out.append({
            "type": "multiple_choice",
            "short_name": letters[i],
            "long_name": c["label"],
            "reply": c["reply"],
        })
    return {
        "widget_type": "zform",
        "extra_data": {"type": "choices", "heading": heading, "choices": out},
    }


def _fallback(heading, choices):
    lines = [heading, ""]
    for i, c in enumerate(choices):
        lines.append(f"  **{'ABCDEFGHIJ'[i]}.** {c['label']}  — reply with `{c['reply']}`")
    return "\n".join(lines)


async def main() -> int:
    site = os.environ.get("ZULIP_SITE")
    email = os.environ.get("ZULIP_EMAIL")
    key = os.environ.get("ZULIP_API_KEY")
    if not (site and email and key):
        print("Missing ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY", file=sys.stderr)
        return 2

    verify = os.environ.get("ZULIP_VERIFY_TLS", "true").lower() not in {"0", "false", "no", "off"}
    widget = _build_zform(HEADING, CHOICES)
    fallback = _fallback(HEADING, CHOICES)

    async with ZulipClient(site, email, key, verify_tls=verify) as c:
        try:
            mid = await c.send_stream_message(
                STREAM, TOPIC, fallback, widget_content=widget,
            )
        except ZulipAPIError as e:
            print(f"Zulip API rejected widget_content: {e}", file=sys.stderr)
            return 1

    url = (
        f"{site.rstrip('/')}/#narrow/stream/{STREAM}"
        f"/topic/{TOPIC}/near/{mid}"
    )
    print(f"posted message_id={mid}")
    print(f"narrow url: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
