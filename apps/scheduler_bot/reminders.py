"""Reminder scheduling via APScheduler, persisted in Postgres.

Design constraints (APScheduler + persistence):
- The job target ``send_reminder`` is a module-global coroutine referenced by
  import path ``reminders:send_reminder`` — stable because the bot runs with
  this directory on ``sys.path``.
- Job args are only primitives (event_id, offset). We never pickle a live
  discord object; the coroutine re-loads everything from the DB / bot cache.
- Each job has an explicit id and ``replace_existing=True`` so restarts don't
  duplicate jobs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from quorum_shared.config import settings
from quorum_shared.db import session_scope
from quorum_shared.models import Event, Rsvp, RsvpStatus

log = logging.getLogger("quorum.reminders")

# Module-global bot handle, set once at startup by bot.py. Reminder jobs use it
# to send messages after being reloaded from the persistent jobstore.
_bot: discord.Bot | None = None

scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=settings.database_url)},
    timezone="UTC",
    job_defaults={"coalesce": True, "misfire_grace_time": 3600},
)


def set_bot(bot: discord.Bot) -> None:
    global _bot
    _bot = bot


def _job_id(event_id: int, offset: int) -> str:
    return f"rem:{event_id}:{offset}"


def _guild_offsets(guild_offsets: str) -> list[int]:
    if guild_offsets.strip():
        try:
            return [int(x) for x in guild_offsets.split(",") if x.strip()]
        except ValueError:
            pass
    return settings.reminder_offsets


def schedule_event_reminders(
    event_id: int, start_at: datetime, guild_offsets: str = ""
) -> None:
    """(Re)schedule all reminder jobs for an event. Past offsets are skipped."""
    now = datetime.now(timezone.utc)
    for offset in _guild_offsets(guild_offsets):
        fire_at = start_at - timedelta(minutes=offset)
        if fire_at <= now:
            continue
        scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=fire_at,
            args=[event_id, offset],
            id=_job_id(event_id, offset),
            replace_existing=True,
        )


def cancel_event_reminders(event_id: int, guild_offsets: str = "") -> None:
    for offset in _guild_offsets(guild_offsets):
        try:
            scheduler.remove_job(_job_id(event_id, offset))
        except Exception:  # job may not exist
            pass


async def send_reminder(event_id: int, offset: int) -> None:
    """Fire a single reminder: ping the event channel and RSVP'd attendees."""
    if _bot is None:
        log.warning("send_reminder called before bot was set")
        return

    def _load() -> tuple[Event, list[int]] | None:
        with session_scope() as s:
            ev = s.get(Event, event_id)
            if ev is None or ev.cancelled:
                return None
            going = [
                r.user_id
                for r in s.query(Rsvp).filter(
                    Rsvp.event_id == event_id, Rsvp.status == RsvpStatus.yes
                )
            ]
            # Detach a lightweight copy of the fields we need.
            s.expunge(ev)
            return ev, going

    loaded = await asyncio.to_thread(_load)
    if loaded is None:
        return
    ev, going = loaded

    from util import humanize_offset, to_discord_ts  # local import avoids cycle

    channel = _bot.get_channel(ev.channel_id)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(ev.channel_id)
        except discord.HTTPException:
            return

    mentions = " ".join(f"<@{uid}>" for uid in going)
    embed = discord.Embed(
        title=f"⏰ Reminder: {ev.title}",
        description=(ev.description or "")[:2000],
        color=discord.Color.orange(),
    )
    embed.add_field(name="Starts", value=to_discord_ts(ev.start_at), inline=True)
    embed.add_field(name="In", value=humanize_offset(offset), inline=True)
    if ev.location:
        embed.add_field(name="Where", value=ev.location, inline=False)
    embed.set_footer(text=f"Event #{ev.id}")

    try:
        await channel.send(content=mentions or None, embed=embed)
    except discord.HTTPException as exc:
        log.warning("failed to send reminder for event %s: %s", event_id, exc)
