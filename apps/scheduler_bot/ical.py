"""FastAPI iCal feed + health endpoint, run in-process with the scheduler bot.

Anyone can subscribe to ``{PUBLIC_BASE_URL}/calendar/{guild_id}/{token}.ics`` in
Google/Apple/Outlook. The per-guild token (stored on GuildSettings) gates access.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from icalendar import Calendar, Event as ICalEvent

from quorum_shared.config import settings
from quorum_shared.db import session_scope
from quorum_shared.models import Event, GuildSettings

app = FastAPI(title="Quorum iCal Feed", docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _build_feed(guild_id: int, token: str) -> str:
    with session_scope() as s:
        gs = s.get(GuildSettings, guild_id)
        if gs is None or gs.ical_token != token:
            raise HTTPException(status_code=404, detail="Calendar not found")

        cal = Calendar()
        cal.add("prodid", "-//Quorum//Discord Meeting Suite//EN")
        cal.add("version", "2.0")
        cal.add("x-wr-calname", f"Quorum {guild_id}")

        events = (
            s.query(Event)
            .filter(Event.guild_id == guild_id, Event.cancelled == False)  # noqa: E712
            .order_by(Event.start_at)
        )
        for ev in events:
            ie = ICalEvent()
            ie.add("uid", f"quorum-{ev.id}@{guild_id}")
            ie.add("summary", ev.title)
            if ev.description:
                ie.add("description", ev.description)
            if ev.location:
                ie.add("location", ev.location)
            ie.add("dtstart", ev.start_at)
            ie.add("dtend", ev.end_at)
            ie.add("dtstamp", datetime.now(timezone.utc))
            cal.add_component(ie)
    return cal.to_ical().decode("utf-8")


@app.get("/calendar/{guild_id}/{token}.ics")
async def calendar_feed(guild_id: int, token: str) -> Response:
    body = await asyncio.to_thread(_build_feed, guild_id, token)
    return Response(content=body, media_type="text/calendar")


async def serve_ical() -> None:
    """Run uvicorn as an asyncio task alongside the Discord bot."""
    config = uvicorn.Config(
        app, host=settings.ical_host, port=settings.ical_port, log_level="warning"
    )
    server = uvicorn.Server(config)
    await server.serve()
