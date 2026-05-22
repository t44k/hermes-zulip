#!/usr/bin/env python
"""M1 smoke test: connect to Zulip, probe identity, send a single message.

Usage:
    ZULIP_SITE=... ZULIP_EMAIL=... ZULIP_API_KEY=... \
        python scripts/smoke_send.py <stream> <topic> <message>

Example:
    python scripts/smoke_send.py sandbox setup-test "👋 from Ange (M1)"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make the plugin importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import ZulipClient, ZulipAPIError  # noqa: E402


async def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    stream, topic, message = sys.argv[1], sys.argv[2], sys.argv[3]

    site = os.getenv("ZULIP_SITE")
    email = os.getenv("ZULIP_EMAIL")
    api_key = os.getenv("ZULIP_API_KEY")
    verify_tls = os.getenv("ZULIP_VERIFY_TLS", "true").lower() not in {"0", "false", "no"}

    if not (site and email and api_key):
        print("Missing ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY in env.", file=sys.stderr)
        return 2

    async with ZulipClient(site, email, api_key, verify_tls=verify_tls) as c:
        try:
            me = await c.get_me()
            print(f"✓ Connected as: {me.get('full_name')} <{me.get('email')}>  "
                  f"(user_id={me.get('user_id')})")

            subs = await c.get_subscriptions()
            print(f"✓ Subscribed to {len(subs)} stream(s):")
            for s in subs[:20]:
                print(f"    #{s['name']}    {s.get('description', '')[:60]}")
            if len(subs) > 20:
                print(f"    … and {len(subs) - 20} more")

            print(f"\n→ Sending to #{stream} > {topic} …")
            msg_id = await c.send_stream_message(stream, topic, message)
            print(f"✓ Sent. message_id={msg_id}")
            return 0
        except ZulipAPIError as e:
            print(f"✗ Zulip API error: {e}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
