"""Slash commands for events, RSVPs, and calendar linking.

NOTE: do NOT add `from __future__ import annotations` here. Pycord introspects
the real annotation objects (discord.Option / discord.TextChannel) at runtime to
build slash options; stringized annotations (PEP 563) break option typing and
raise `issubclass() arg 1 must be a class` on invoke.
"""

import asyncio
import secrets
from datetime import date, datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import SlashCommandGroup

from quorum_shared import gcal
from quorum_shared.config import settings
from quorum_shared.db import session_scope
from quorum_shared.models import Event, GuildSettings, Rsvp, RsvpStatus

import reminders
from util import fmt_local, get_tz, parse_duration_minutes, to_discord_ts

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

    @event.command(name="create", description="Schedule a new meeting (pick date & time)")
    async def create(
        self,
        ctx: discord.ApplicationContext,
        title: str,
        duration: discord.Option(str, "e.g. 60, 1h, 1h30m") = "60",
        description: discord.Option(str, "Optional details") = "",
        location: discord.Option(str, "Optional location / link") = "",
        channel: discord.Option(discord.TextChannel, "Where to post & remind (default: here)") = None,
    ):
        tz = await asyncio.to_thread(_guild_tz, ctx.guild.id)
        minutes = parse_duration_minutes(duration)
        target_channel = channel or ctx.channel
        view = EventCreateView(
            title=title,
            duration_min=minutes,
            description=description,
            location=location,
            channel=target_channel,
            guild_id=ctx.guild.id,
            tz_name=tz,
            author_id=ctx.author.id,
        )
        await ctx.respond(
            f"🗓️ Pick a date and time for **{title}** ({minutes} min), then press **Create event**:",
            view=view,
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


async def create_event_flow(
    *, guild_id, channel, author_id, title, description, location, start_utc, duration_min, tz_name
) -> int:
    """Persist an event, push to Google Calendar, announce it, and arm reminders."""
    end_utc = start_utc + timedelta(minutes=duration_min)

    def _persist() -> tuple[int, str]:
        with session_scope() as s:
            gs = _get_or_create_settings(s, guild_id)
            gcal_id = gs.gcal_id
            ev = Event(
                guild_id=guild_id,
                channel_id=channel.id,
                title=title,
                description=description,
                location=location,
                start_at=start_utc,
                end_at=end_utc,
                creator_id=author_id,
            )
            s.add(ev)
            s.flush()
            event_id = ev.id
            offsets = gs.reminder_offsets
            if gcal_id:
                ev.gcal_event_id = gcal.upsert_event(
                    gcal_id,
                    title=title,
                    description=description,
                    location=location,
                    start=start_utc,
                    end=end_utc,
                )
            return event_id, offsets

    event_id, offsets = await asyncio.to_thread(_persist)

    embed = build_event_embed_stub(event_id, title, description, location, start_utc, end_utc, tz_name)
    msg = await channel.send(embed=embed, view=RsvpView(event_id))

    def _save_msg() -> None:
        with session_scope() as s:
            ev = s.get(Event, event_id)
            if ev:
                ev.announce_message_id = msg.id

    await asyncio.to_thread(_save_msg)
    reminders.schedule_event_reminders(event_id, start_utc, offsets)
    return event_id


class EventCreateView(discord.ui.View):
    """Ephemeral date/time picker: Day + Hour + Minute selects, then Create."""

    def __init__(self, *, title, duration_min, description, location, channel, guild_id, tz_name, author_id):
        super().__init__(timeout=300)
        self.title = title
        self.duration_min = duration_min
        self.description = description
        self.location = location
        self.channel = channel
        self.guild_id = guild_id
        self.tz_name = tz_name
        self.author_id = author_id
        self.sel_day = None
        self.sel_hour = None
        self.sel_minute = 0

        today = datetime.now(get_tz(tz_name)).date()
        day_opts = [
            discord.SelectOption(
                label=(today + timedelta(days=i)).strftime("%a %b %d"),
                value=(today + timedelta(days=i)).isoformat(),
            )
            for i in range(14)  # Discord caps selects at 25 options
        ]
        self.day_select = discord.ui.Select(placeholder="📅 Pick a day", options=day_opts, row=0)
        self.day_select.callback = self._on_day
        self.add_item(self.day_select)

        hour_opts = [discord.SelectOption(label=f"{h:02d}:00", value=str(h)) for h in range(24)]
        self.hour_select = discord.ui.Select(placeholder="🕒 Pick an hour", options=hour_opts, row=1)
        self.hour_select.callback = self._on_hour
        self.add_item(self.hour_select)

        min_opts = [discord.SelectOption(label=f":{m:02d}", value=str(m)) for m in (0, 15, 30, 45)]
        self.minute_select = discord.ui.Select(placeholder="Minutes (:00)", options=min_opts, row=2)
        self.minute_select.callback = self._on_minute
        self.add_item(self.minute_select)

        create_btn = discord.ui.Button(label="Create event", style=discord.ButtonStyle.success, row=3)
        create_btn.callback = self._on_create
        self.add_item(create_btn)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use this.", ephemeral=True
            )
            return False
        return True

    async def _on_day(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.sel_day = self.day_select.values[0]
        await interaction.response.defer()

    async def _on_hour(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.sel_hour = int(self.hour_select.values[0])
        await interaction.response.defer()

    async def _on_minute(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.sel_minute = int(self.minute_select.values[0])
        await interaction.response.defer()

    async def _on_create(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        if self.sel_day is None or self.sel_hour is None:
            await interaction.response.send_message("Pick a day and an hour first.", ephemeral=True)
            return
        d = date.fromisoformat(self.sel_day)
        local_dt = datetime(
            d.year, d.month, d.day, self.sel_hour, self.sel_minute, tzinfo=get_tz(self.tz_name)
        )
        start_utc = local_dt.astimezone(timezone.utc)
        await interaction.response.defer()
        event_id = await create_event_flow(
            guild_id=self.guild_id,
            channel=self.channel,
            author_id=self.author_id,
            title=self.title,
            description=self.description,
            location=self.location,
            start_utc=start_utc,
            duration_min=self.duration_min,
            tz_name=self.tz_name,
        )
        self.disable_all_items()
        self.stop()
        await interaction.edit_original_response(
            content=(
                f"✅ Created **{self.title}** (event #{event_id}) in {self.channel.mention} "
                f"for {fmt_local(start_utc, self.tz_name)}."
            ),
            view=None,
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Events(bot))
