"""Quorum Scheduler — calendar, events, reminders, and iCal feed."""

from __future__ import annotations

import asyncio
import logging

import discord

from quorum_shared.config import settings
from quorum_shared.db import init_db

import reminders
from cogs.events import handle_rsvp_interaction
from ical import serve_ical

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("quorum.scheduler")

intents = discord.Intents.default()  # guilds is enough; no message content needed
bot = discord.Bot(intents=intents)

_started = False


@bot.event
async def on_ready() -> None:
    global _started
    log.info("Scheduler logged in as %s (id=%s)", bot.user, bot.user.id)
    if _started:
        return
    _started = True

    reminders.set_bot(bot)
    if not reminders.scheduler.running:
        reminders.scheduler.start()
        log.info("APScheduler started")

    # Serve the iCal feed + healthz alongside the bot.
    asyncio.create_task(serve_ical())
    log.info("iCal feed serving on %s:%s", settings.ical_host, settings.ical_port)


@bot.listen("on_interaction")
async def _rsvp_listener(interaction: discord.Interaction) -> None:
    # Additional listener; does not replace Pycord's app-command dispatch.
    if interaction.type == discord.InteractionType.component:
        await handle_rsvp_interaction(interaction)


def main() -> None:
    if not settings.discord_token_scheduler:
        raise SystemExit("DISCORD_TOKEN_SCHEDULER is not set.")
    init_db()
    bot.load_extension("cogs.events")
    bot.run(settings.discord_token_scheduler)


if __name__ == "__main__":
    main()
