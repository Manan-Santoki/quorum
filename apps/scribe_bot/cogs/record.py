"""/record slash commands: capture Discord voice and produce AI minutes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import SlashCommandGroup

from quorum_shared.config import settings
from quorum_shared.db import session_scope
from quorum_shared.models import Event, Minutes, Recording, RecordingStatus, Transcript

from pipeline import PipelineResult, Track, run_pipeline

log = logging.getLogger("quorum.record")
_GUILD_IDS = settings.dev_guild_ids or None


class Record(commands.Cog):
    record = SlashCommandGroup("record", "Record a voice meeting and get minutes", guild_ids=_GUILD_IDS)

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # guild_id -> {"vc", "recording_id", "event_id", "text_channel_id", "autostop"}
        self.sessions: dict[int, dict] = {}

    # ----------------------------------------------------------------- start
    @record.command(name="start", description="Join your voice channel and start recording")
    async def start(
        self,
        ctx: discord.ApplicationContext,
        event_id: discord.Option(int, "Link this recording to an event #", required=False, default=None),
    ):
        if not (ctx.author.voice and ctx.author.voice.channel):
            await ctx.respond("Join a voice channel first, then run `/record start`.", ephemeral=True)
            return
        if ctx.guild.id in self.sessions:
            await ctx.respond("Already recording in this server. Use `/record stop`.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        voice_channel = ctx.author.voice.channel

        # Validate event linkage (must belong to this guild).
        if event_id is not None:
            valid = await asyncio.to_thread(self._event_belongs, event_id, ctx.guild.id)
            if not valid:
                await ctx.respond(f"No event #{event_id} in this server.", ephemeral=True)
                return

        try:
            vc = await voice_channel.connect()
        except discord.ClientException:
            await ctx.respond("I'm already connected to a voice channel here.", ephemeral=True)
            return

        recording_id = await asyncio.to_thread(
            self._create_recording, ctx.guild.id, ctx.channel.id, voice_channel.id, ctx.author.id, event_id
        )

        autostop = asyncio.create_task(self._auto_stop(ctx.guild.id))
        self.sessions[ctx.guild.id] = {
            "vc": vc,
            "recording_id": recording_id,
            "event_id": event_id,
            "text_channel_id": ctx.channel.id,
            "autostop": autostop,
        }

        vc.start_recording(discord.sinks.WaveSink(), self._on_finished, ctx.guild.id)

        # Visible consent notice in the channel.
        await ctx.channel.send(
            f"🔴 **Recording started** in {voice_channel.mention} by {ctx.author.mention}. "
            "Everyone in the channel is being recorded for meeting minutes."
        )
        await ctx.respond(f"Recording started (recording #{recording_id}).", ephemeral=True)

    # ------------------------------------------------------------------ stop
    @record.command(name="stop", description="Stop recording and generate minutes")
    async def stop(self, ctx: discord.ApplicationContext):
        session = self.sessions.get(ctx.guild.id)
        if not session:
            await ctx.respond("I'm not recording in this server.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        session["vc"].stop_recording()  # triggers _on_finished
        await ctx.respond("⏹️ Stopped. Transcribing and writing minutes… this can take a bit.", ephemeral=True)

    # ------------------------------------------------------------- callbacks
    async def _auto_stop(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(settings.max_recording_minutes * 60)
        except asyncio.CancelledError:
            return
        session = self.sessions.get(guild_id)
        if session:
            log.info("Auto-stopping recording in guild %s (max duration reached)", guild_id)
            try:
                session["vc"].stop_recording()
            except Exception:
                pass

    async def _on_finished(self, sink: discord.sinks.WaveSink, guild_id: int) -> None:
        session = self.sessions.pop(guild_id, None)
        if session and session.get("autostop"):
            session["autostop"].cancel()

        vc = sink.vc
        guild = vc.guild if vc else self.bot.get_guild(guild_id)
        text_channel_id = session["text_channel_id"] if session else None
        recording_id = session["recording_id"] if session else None
        event_id = session["event_id"] if session else None

        channel = self.bot.get_channel(text_channel_id) if text_channel_id else None
        if channel is None and text_channel_id:
            try:
                channel = await self.bot.fetch_channel(text_channel_id)
            except discord.HTTPException:
                channel = None

        try:
            await vc.disconnect()
        except Exception:
            pass

        if recording_id is not None:
            await asyncio.to_thread(self._mark_status, recording_id, RecordingStatus.processing)

        # Persist per-speaker WAVs to a temp workdir.
        workdir = tempfile.mkdtemp(prefix="quorum_rec_")
        tracks: list[Track] = []
        for user_id, audio in sink.audio_data.items():
            name = self._display_name(guild, user_id)
            wav_path = os.path.join(workdir, f"{user_id}.wav")
            try:
                audio.file.seek(0)
                with open(wav_path, "wb") as fh:
                    fh.write(audio.file.read())
                tracks.append(Track(user_id=user_id, name=name, wav_path=wav_path))
            except Exception as exc:
                log.warning("failed to write track for %s: %s", user_id, exc)

        if not tracks:
            if channel:
                await channel.send("⚠️ Recording finished but no audio was captured.")
            if recording_id is not None:
                await asyncio.to_thread(self._mark_status, recording_id, RecordingStatus.failed, "no audio")
            return

        try:
            result: PipelineResult = await asyncio.to_thread(run_pipeline, tracks, workdir)
        except Exception as exc:
            log.exception("pipeline failed")
            if channel:
                await channel.send(f"❌ Failed to process the recording: `{exc}`")
            if recording_id is not None:
                await asyncio.to_thread(self._mark_status, recording_id, RecordingStatus.failed, str(exc))
            return

        await self._post_results(channel, result, recording_id, event_id)

    # -------------------------------------------------------------- posting
    async def _post_results(self, channel, result: PipelineResult, recording_id, event_id) -> None:
        m = result.minutes
        embed = discord.Embed(title="📝 Meeting Minutes", description=m.summary[:4000], color=discord.Color.green())
        if m.decisions:
            embed.add_field(
                name="Decisions", value="\n".join(f"• {d}" for d in m.decisions)[:1024], inline=False
            )
        if m.action_items:
            lines = []
            for a in m.action_items:
                owner = f"**{a['owner']}** " if a.get("owner") else ""
                due = f" _(due {a['due']})_" if a.get("due") else ""
                lines.append(f"• {owner}{a['task']}{due}")
            embed.add_field(name="Action Items", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text=f"Recording #{recording_id} • {result.speaker_count} speaker(s)")

        transcript_file = discord.File(
            io_bytes(result.transcript), filename=f"transcript_{recording_id}.txt"
        )

        posted_message_id = None
        if channel:
            msg = await channel.send(embed=embed, file=transcript_file)
            posted_message_id = msg.id
            if result.warnings:
                await channel.send("⚠️ " + "; ".join(result.warnings[:5]))

        if recording_id is not None:
            await asyncio.to_thread(
                self._persist_results, recording_id, result, posted_message_id
            )

    # ------------------------------------------------------------------ DB
    @staticmethod
    def _event_belongs(event_id: int, guild_id: int) -> bool:
        with session_scope() as s:
            ev = s.get(Event, event_id)
            return ev is not None and ev.guild_id == guild_id

    @staticmethod
    def _create_recording(guild_id, channel_id, voice_channel_id, started_by, event_id) -> int:
        with session_scope() as s:
            rec = Recording(
                guild_id=guild_id,
                channel_id=channel_id,
                voice_channel_id=voice_channel_id,
                started_by=started_by,
                event_id=event_id,
                status=RecordingStatus.recording,
            )
            s.add(rec)
            s.flush()
            return rec.id

    @staticmethod
    def _mark_status(recording_id: int, status: RecordingStatus, error: str | None = None) -> None:
        with session_scope() as s:
            rec = s.get(Recording, recording_id)
            if rec:
                rec.status = status
                if error:
                    rec.error = error
                if status in (RecordingStatus.complete, RecordingStatus.failed):
                    rec.ended_at = datetime.now(timezone.utc)

    @staticmethod
    def _persist_results(recording_id: int, result: PipelineResult, posted_message_id) -> None:
        with session_scope() as s:
            s.add(Transcript(recording_id=recording_id, text=result.transcript))
            s.add(
                Minutes(
                    recording_id=recording_id,
                    summary=result.minutes.summary,
                    decisions_json=json.dumps(result.minutes.decisions),
                    action_items_json=json.dumps(result.minutes.action_items),
                    posted_message_id=posted_message_id,
                )
            )
            rec = s.get(Recording, recording_id)
            if rec:
                rec.status = RecordingStatus.complete
                rec.ended_at = datetime.now(timezone.utc)

    def _display_name(self, guild, user_id: int) -> str:
        if guild:
            member = guild.get_member(user_id)
            if member:
                return member.display_name
        return f"User-{user_id}"


def io_bytes(text: str):
    import io

    return io.BytesIO(text.encode("utf-8"))


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Record(bot))
