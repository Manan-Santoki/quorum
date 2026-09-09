"""Optional Google Calendar push via a service account.

Setup (documented in README): create a service account, download its JSON key,
mount it into the container and point ``GOOGLE_SA_JSON`` at it. Share the target
Google Calendar with the service-account email (Make changes to events), then
run ``/calendar link <calendar_id>`` in the guild.

If ``GOOGLE_SA_JSON`` is unset, ``is_enabled()`` is False and callers skip
Google entirely — the internal Postgres calendar remains the source of truth.
"""

from __future__ import annotations

import os
from datetime import datetime

from .config import settings

_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def is_enabled() -> bool:
    return bool(settings.google_sa_json) and os.path.exists(settings.google_sa_json)


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        settings.google_sa_json, scopes=_SCOPES
    )
    # cache_discovery=False avoids a noisy warning and a file-cache dependency.
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _body(title: str, description: str, location: str, start: datetime, end: datetime) -> dict:
    return {
        "summary": title,
        "description": description or "",
        "location": location or "",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def upsert_event(
    calendar_id: str,
    *,
    title: str,
    description: str,
    location: str,
    start: datetime,
    end: datetime,
    gcal_event_id: str | None = None,
) -> str | None:
    """Create or update a Google Calendar event; return its Google event id.

    Any Google API error is swallowed and returns None — Google push must never
    break the primary (Discord + Postgres) flow.
    """
    if not is_enabled():
        return None
    try:
        svc = _service()
        body = _body(title, description, location, start, end)
        if gcal_event_id:
            ev = (
                svc.events()
                .update(calendarId=calendar_id, eventId=gcal_event_id, body=body)
                .execute()
            )
        else:
            ev = svc.events().insert(calendarId=calendar_id, body=body).execute()
        return ev.get("id")
    except Exception:  # pragma: no cover - network/credentials dependent
        return None


def delete_event(calendar_id: str, gcal_event_id: str) -> None:
    if not is_enabled() or not gcal_event_id:
        return
    try:
        svc = _service()
        svc.events().delete(calendarId=calendar_id, eventId=gcal_event_id).execute()
    except Exception:  # pragma: no cover
        return
