"""Quorum Scribe — records Discord voice and posts AI-generated minutes."""

from __future__ import annotations

import logging

import discord

from quorum_shared.config import settings
from quorum_shared.db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("quorum.scribe")

intents = discord.Intents.default()
intents.voice_states = True  # required to know who is in the voice channel
intents.members = True       # privileged: resolve display names (enable in Dev Portal)

bot = discord.Bot(intents=intents)


@bot.event
async def on_ready() -> None:
    log.info("Scribe logged in as %s (id=%s)", bot.user, bot.user.id)


def main() -> None:
    if not settings.discord_token_scribe:
        raise SystemExit("DISCORD_TOKEN_SCRIBE is not set.")
    init_db()  # idempotent; ensures tables exist even if scribe starts first
    bot.load_extension("cogs.record")
    bot.run(settings.discord_token_scribe)


if __name__ == "__main__":
    main()
