"""Slash commands for events, RSVPs, and calendar linking."""

from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta

import discord
from discord.ext import commands
from discord import SlashCommandGroup

from quorum_shared import gcal
from quorum_shared.config import settings
from quorum_shared.db import session_scope
from quorum_shared.models import Event, GuildSettings, Rsvp, RsvpStatus

import reminders
from util import fmt_local, parse_duration_minutes, parse_when, to_discord_ts

_GUILD_IDS = settings.dev_guild_ids or None  # None => global registration


# --------------------------------------------------------------------------- #
# DB helpers (run via asyncio.to_thread from async command handlers)
# --------------------------------------------------------------------------- #
def _get_or_create_settings(session, guild_id: int) -> GuildSettings:
    gs = session.get(GuildSettings, guild_id)
    if gs is None:
        gs = GuildSettings(
            guild_id=guild_id,
            timezone=settings.default_timezone,
            ical_token=secrets.token_urlsafe(16),
        )
        session.add(gs)
        session.flush()
    return gs


def _rsvp_counts(session, event_id: int) -> dict[str, int]:
    counts = {"yes": 0, "no": 0, "maybe": 0}
    for r in session.query(Rsvp).filter(Rsvp.event_id == event_id):
        counts[r.status.value] += 1
    return counts


def _guild_tz(guild_id: int) -> str:
    with session_scope() as s:
        gs = _get_or_create_settings(s, guild_id)
        return gs.timezone


def build_event_embed(ev: Event, counts: dict[str, int], tz_name: str) -> discord.Embed:
    color = discord.Color.dark_gray() if ev.cancelled else discord.Color.blurple()
    title = ("❌ [Cancelled] " if ev.cancelled else "📅 ") + ev.title
    embed = discord.Embed(title=title, description=(ev.description or "")[:2000], color=color)
    embed.add_field(name="When", value=to_discord_ts(ev.start_at), inline=False)
    embed.add_field(
        name="Local", value=fmt_local(ev.start_at, tz_name), inline=True
    )
    duration = int((ev.end_at - ev.start_at).total_seconds() // 60)
    embed.add_field(name="Duration", value=f"{duration} min", inline=True)
    if ev.location:
        embed.add_field(name="Where", value=ev.location, inline=False)
    embed.add_field(
        name="RSVP",
        value=f"✅ {counts['yes']}  ❔ {counts['maybe']}  ❌ {counts['no']}",
        inline=False,
    )
    embed.set_footer(text=f"Event #{ev.id} • RSVP with the buttons below")
    return embed


# --------------------------------------------------------------------------- #
# Persistent RSVP buttons
# --------------------------------------------------------------------------- #
class RsvpView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        for status, label, emoji in (
            ("yes", "Going", "✅"),
            ("maybe", "Maybe", "❔"),
            ("no", "Can't", "❌"),
        ):
            self.add_item(
                discord.ui.Button(
                    label=label,
                    emoji=emoji,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"rsvp:{status}:{event_id}",
                )
            )


def _apply_rsvp(event_id: int, user_id: int, status: str) -> tuple[Event, dict[str, int], str] | None:
    with session_scope() as s:
        ev = s.get(Event, event_id)
        if ev is None or ev.cancelled:
            return None
        row = (
            s.query(Rsvp)
            .filter(Rsvp.event_id == event_id, Rsvp.user_id == user_id)
            .one_or_none()
        )
        if row is None:
            s.add(Rsvp(event_id=event_id, user_id=user_id, status=RsvpStatus(status)))
        else:
            row.status = RsvpStatus(status)
        s.flush()
        counts = _rsvp_counts(s, event_id)
        gs = _get_or_create_settings(s, ev.guild_id)
        tz = gs.timezone
        s.expunge(ev)
        return ev, counts, tz


async def handle_rsvp_interaction(interaction: discord.Interaction) -> bool:
    """Global interaction hook: process ``rsvp:<status>:<event_id>`` buttons.

    Returns True if it handled the interaction.
    """
    data = interaction.data or {}
    custom_id = data.get("custom_id", "")
    if not custom_id.startswith("rsvp:"):
        return False
    _, status, raw_id = custom_id.split(":", 2)
    event_id = int(raw_id)

    result = await asyncio.to_thread(_apply_rsvp, event_id, interaction.user.id, status)
    if result is None:
        await interaction.response.send_message("That event is no longer available.", ephemeral=True)
        return True
    ev, counts, tz = result
    try:
        await interaction.response.edit_message(embed=build_event_embed(ev, counts, tz))
    except discord.HTTPException:
        await interaction.response.send_message(f"RSVP recorded: **{status}**", ephemeral=True)
    return True


def reattach_views(bot: discord.Bot) -> None:
    """Re-register persistent RSVP views for active events after a restart."""
    with session_scope() as s:
        rows = (
            s.query(Event)
            .filter(Event.cancelled == False, Event.announce_message_id.isnot(None))  # noqa: E712
            .all()
        )
        pairs = [(e.id, e.announce_message_id) for e in rows]
    for event_id, message_id in pairs:
        bot.add_view(RsvpView(event_id), message_id=message_id)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Events(commands.Cog):
    event = SlashCommandGroup("event", "Create and manage meetings", guild_ids=_GUILD_IDS)
    calendar = SlashCommandGroup("calendar", "Calendar integration", guild_ids=_GUILD_IDS)

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @event.command(name="create", description="Schedule a new meeting")
    async def create(
        self,
        ctx: discord.ApplicationContext,
        title: str,
        when: discord.Option(str, "Start time, e.g. '2026-09-12 15:00' or 'Friday 3pm'"),
        duration: discord.Option(str, "e.g. 60, 1h, 1h30m", required=False, default="60"),
        description: discord.Option(str, "Optional details", required=False, default=""),
        location: discord.Option(str, "Optional location / link", required=False, default=""),
        channel: discord.Option(
            discord.TextChannel, "Where to post & remind (default: here)", required=False, default=None
        ),
    ):
        await ctx.defer()
        tz = await asyncio.to_thread(_guild_tz, ctx.guild.id)
        try:
            start = parse_when(when, tz)
        except (ValueError, OverflowError):
            await ctx.respond(
                f"Couldn't understand the time `{when}`. Try `2026-09-12 15:00`.", ephemeral=True
            )
            return
        minutes = parse_duration_minutes(duration)
        end = start + timedelta(minutes=minutes)
        target_channel = channel or ctx.channel

        def _create() -> tuple[int, str, str | None]:
            with session_scope() as s:
                gs = _get_or_create_settings(s, ctx.guild.id)
                gcal_id = gs.gcal_id
                ev = Event(
                    guild_id=ctx.guild.id,
                    channel_id=target_channel.id,
                    title=title,
                    description=description,
                    location=location,
                    start_at=start,
                    end_at=end,
                    creator_id=ctx.author.id,
                )
                s.add(ev)
                s.flush()
                event_id = ev.id
                offsets = gs.reminder_offsets
                if gcal_id:
                    gid = gcal.upsert_event(
                        gcal_id,
                        title=title,
                        description=description,
                        location=location,
                        start=start,
                        end=end,
                    )
                    ev.gcal_event_id = gid
                return event_id, offsets, gcal_id

        event_id, offsets, _gcal_id = await asyncio.to_thread(_create)

        # Announce with RSVP buttons.
        embed = build_event_embed_stub(event_id, title, description, location, start, end, tz)
        view = RsvpView(event_id)
        msg = await target_channel.send(embed=embed, view=view)

        def _save_msg() -> None:
            with session_scope() as s:
                ev = s.get(Event, event_id)
                if ev:
                    ev.announce_message_id = msg.id

        await asyncio.to_thread(_save_msg)
        reminders.schedule_event_reminders(event_id, start, offsets)

        await ctx.respond(
            f"✅ Created **{title}** (event #{event_id}) in {target_channel.mention}.",
            ephemeral=True,
        )

    @event.command(name="list", description="List upcoming meetings")
    async def list_events(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)

        def _load():
            from datetime import datetime, timezone

            with session_scope() as s:
                gs = _get_or_create_settings(s, ctx.guild.id)
                rows = (
                    s.query(Event)
                    .filter(
                        Event.guild_id == ctx.guild.id,
                        Event.cancelled == False,  # noqa: E712
                        Event.end_at >= datetime.now(timezone.utc),
                    )
                    .order_by(Event.start_at)
                    .limit(15)
                    .all()
                )
                return [(e.id, e.title, e.start_at) for e in rows], gs.timezone

        rows, tz = await asyncio.to_thread(_load)
        if not rows:
            await ctx.respond("No upcoming meetings. Create one with `/event create`.", ephemeral=True)
            return
        lines = [
            f"**#{eid}** — {title} · {to_discord_ts(start, 'F')} ({fmt_local(start, tz)})"
            for eid, title, start in rows
        ]
        await ctx.respond("\n".join(lines), ephemeral=True)

    @event.command(name="cancel", description="Cancel a meeting")
    async def cancel(self, ctx: discord.ApplicationContext, event_id: int):
        await ctx.defer(ephemeral=True)

        def _cancel() -> tuple[bool, str | None, str, int | None, int | None]:
            with session_scope() as s:
                ev = s.get(Event, event_id)
                if ev is None or ev.guild_id != ctx.guild.id:
                    return False, None, "", None, None
                ev.cancelled = True
                gs = _get_or_create_settings(s, ev.guild_id)
                if ev.gcal_event_id and gs.gcal_id:
                    gcal.delete_event(gs.gcal_id, ev.gcal_event_id)
                return True, gs.reminder_offsets, ev.channel_id, ev.announce_message_id, ev.channel_id

        ok, offsets, _chan, msg_id, chan_id = await asyncio.to_thread(_cancel)
        if not ok:
            await ctx.respond(f"No event #{event_id} in this server.", ephemeral=True)
            return
        reminders.cancel_event_reminders(event_id, offsets or "")

        # Best-effort: strike through the announcement.
        if msg_id and chan_id:
            try:
                ch = self.bot.get_channel(chan_id) or await self.bot.fetch_channel(chan_id)
                msg = await ch.fetch_message(msg_id)
                if msg.embeds:
                    e = msg.embeds[0]
                    e.color = discord.Color.dark_gray()
                    e.title = "❌ [Cancelled] " + (e.title or "")
                    await msg.edit(embed=e, view=None)
            except discord.HTTPException:
                pass
        await ctx.respond(f"🗑️ Cancelled event #{event_id}.", ephemeral=True)

    @calendar.command(name="link", description="Link a Google Calendar (share it with the service account first)")
    async def link(self, ctx: discord.ApplicationContext, calendar_id: str):
        if not gcal.is_enabled():
            await ctx.respond(
                "Google Calendar isn't configured on this server (no `GOOGLE_SA_JSON`).",
                ephemeral=True,
            )
            return

        def _set():
            with session_scope() as s:
                gs = _get_or_create_settings(s, ctx.guild.id)
                gs.gcal_id = calendar_id

        await asyncio.to_thread(_set)
        await ctx.respond(f"🔗 Linked Google Calendar `{calendar_id}`.", ephemeral=True)

    @calendar.command(name="feed", description="Get this server's iCal subscription URL")
    async def feed(self, ctx: discord.ApplicationContext):
        def _token():
            with session_scope() as s:
                gs = _get_or_create_settings(s, ctx.guild.id)
                return gs.ical_token

        token = await asyncio.to_thread(_token)
        url = f"{settings.public_base_url}/calendar/{ctx.guild.id}/{token}.ics"
        await ctx.respond(
            f"📆 Subscribe in Google/Apple/Outlook with this URL (keep it private):\n{url}",
            ephemeral=True,
        )


def build_event_embed_stub(event_id, title, description, location, start, end, tz) -> discord.Embed:
    """Build the initial announcement embed (no RSVPs yet)."""
    color = discord.Color.blurple()
    embed = discord.Embed(title="📅 " + title, description=(description or "")[:2000], color=color)
    embed.add_field(name="When", value=to_discord_ts(start), inline=False)
    embed.add_field(name="Local", value=fmt_local(start, tz), inline=True)
    duration = int((end - start).total_seconds() // 60)
    embed.add_field(name="Duration", value=f"{duration} min", inline=True)
    if location:
        embed.add_field(name="Where", value=location, inline=False)
    embed.add_field(name="RSVP", value="✅ 0  ❔ 0  ❌ 0", inline=False)
    embed.set_footer(text=f"Event #{event_id} • RSVP with the buttons below")
    return embed


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Events(bot))
