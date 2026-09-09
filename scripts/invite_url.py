#!/usr/bin/env python3
"""Print OAuth2 invite URLs for both bots.

Usage:
    python scripts/invite_url.py <SCHEDULER_APP_ID> <SCRIBE_APP_ID>

Application IDs come from the Discord Developer Portal (General Information).
"""

from __future__ import annotations

import sys

# Permission bit fields (https://discord.com/developers/docs/topics/permissions)
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
MENTION_EVERYONE = 1 << 17
MANAGE_THREADS = 1 << 34
SEND_MESSAGES_IN_THREADS = 1 << 38
CONNECT = 1 << 20
SPEAK = 1 << 21

SCHEDULER_PERMS = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | MANAGE_THREADS | SEND_MESSAGES_IN_THREADS
SCRIBE_PERMS = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | ATTACH_FILES | CONNECT | SPEAK

BASE = "https://discord.com/api/oauth2/authorize"


def url(app_id: str, perms: int) -> str:
    return f"{BASE}?client_id={app_id}&scope=bot%20applications.commands&permissions={perms}"


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(1)
    scheduler_id, scribe_id = sys.argv[1], sys.argv[2]
    print("Quorum Scheduler invite:")
    print("  " + url(scheduler_id, SCHEDULER_PERMS))
    print()
    print("Quorum Scribe invite (enable the 'Server Members' + 'Voice' intents in the portal):")
    print("  " + url(scribe_id, SCRIBE_PERMS))


if __name__ == "__main__":
    main()
