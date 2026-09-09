# Quorum — Discord Meeting Suite

Two open-source, self-hostable Discord bots in one monorepo:

- **Quorum Scheduler** — create meetings with slash commands, RSVP buttons, automatic reminders so nobody forgets, a subscribe-able iCal feed, and optional Google Calendar push.
- **Quorum Scribe** — records a Discord voice channel (one track per speaker), transcribes it, and posts **AI-generated minutes** (summary, decisions, action items) plus the full transcript.

Both share one Postgres database, so a recording can be linked to a scheduled event. Everything deploys with `docker compose up -d`. MIT licensed.

---

## How it works

```
Scheduler bot ──┐                         ┌── Groq Whisper (transcription)
 /event, RSVP,  │                         │
 reminders,     ├── Postgres (shared) ──┤
 iCal, GCal     │                         │
Scribe bot ─────┘                         └── DeepSeek (minutes)
 /record → per-speaker WAV → chunk → transcribe → merge → minutes
```

- **Recording** uses Pycord's native `WaveSink`, which yields one WAV per speaker — so speaker labels come for free, no diarization model.
- Each speaker track is re-encoded to 16 kHz mono and split into ≤8-minute chunks (keeps every upload under Groq's 25 MB limit); chunk timestamps are offset back to the meeting clock so the merged transcript stays correctly ordered across speakers.
- **Transcription** and **minutes** are pluggable (see providers below); defaults are Groq `whisper-large-v3-turbo` and DeepSeek `deepseek-v4-flash`.

## Requirements

- Docker + Docker Compose
- Two Discord applications (two bot tokens)
- A **Groq** API key (transcription) and a **DeepSeek** API key (minutes). OpenRouter or local `faster-whisper` can replace either.
- (Optional) A Google service-account key for Google Calendar push.

## Quick start

```bash
git clone <your-fork> quorum && cd quorum
cp .env.example .env
# edit .env: Postgres password, both Discord tokens, GROQ_API_KEY, DEEPSEEK_API_KEY
docker compose up -d --build
docker compose logs -f scheduler-bot scribe-bot
```

`/healthz` is served at `http://<host>:8080/healthz`.

## Discord app setup

1. In the **Developer Portal**, create **two** applications: e.g. `Quorum Scheduler` and `Quorum Scribe`. Add a bot to each, copy each **bot token** into `.env` (`DISCORD_TOKEN_SCHEDULER`, `DISCORD_TOKEN_SCRIBE`).
2. For **Scribe**, under *Bot → Privileged Gateway Intents*, enable **Server Members Intent** (used to label speakers). Voice receive itself needs no privileged intent.
3. Generate invite links:
   ```bash
   python scripts/invite_url.py <SCHEDULER_APP_ID> <SCRIBE_APP_ID>
   ```
   Invite both bots. Scheduler needs Send Messages / Embed Links / Manage Threads; Scribe needs View Channel / Connect / Speak / Attach Files.
4. **Dev tip:** set `DEV_GUILD_IDS=<your test guild id>` in `.env` for **instant** slash-command registration. Leave it empty in production (global commands, up to ~1h to appear).

## Commands

**Scheduler**
| Command | What it does |
|---|---|
| `/event create title when [duration] [description] [location] [channel]` | Schedule a meeting; posts an announcement with RSVP buttons and arms reminders. `when` accepts `2026-09-12 15:00` or `Friday 3pm`; `duration` accepts `60`, `1h`, `1h30m`. |
| `/event list` | Upcoming meetings in this server. |
| `/event cancel <event_id>` | Cancel a meeting (clears reminders, removes the Google event). |
| `/calendar link <calendar_id>` | Link a Google Calendar (share it with the service account first). |
| `/calendar feed` | Get this server's private iCal subscription URL. |

**Scribe**
| Command | What it does |
|---|---|
| `/record start [event_id]` | Join your voice channel and start recording (optionally linked to an event). Posts a visible recording notice. |
| `/record stop` | Stop, transcribe, and post minutes + transcript. |

Reminders fire at `REMINDER_OFFSETS` (default `1440,60,10` = 1 day / 1 hour / 10 min before) and ping everyone who RSVP'd ✅.

## Providers

Set in `.env`, no code changes:

- `TRANSCRIBE_PROVIDER=groq` (default) — needs `GROQ_API_KEY`. `whisper-large-v3-turbo`, ~$0.04/audio-hour.
- `TRANSCRIBE_PROVIDER=openrouter` — needs `OPENROUTER_API_KEY`; set `OPENROUTER_TRANSCRIBE_MODEL`.
- `TRANSCRIBE_PROVIDER=faster_whisper` — fully local/offline. Add `RUN pip install "faster-whisper>=1.2.1"` to `apps/scribe_bot/Dockerfile` and rebuild. GPU-less VPS: keep `FASTER_WHISPER_MODEL=distil-large-v3` or smaller (large-v3 on CPU is slow).
- `MINUTES_PROVIDER=deepseek` (default) — needs `DEEPSEEK_API_KEY`. `deepseek-v4-flash`, 1M context.
- `MINUTES_PROVIDER=openrouter` — routes minutes through OpenRouter instead.

## Google Calendar (optional)

1. Create a Google Cloud service account, enable the Calendar API, download its JSON key.
2. Save it at `./secrets/google-sa.json` and set `GOOGLE_SA_JSON=/secrets/google-sa.json` in `.env`.
3. Share your Google Calendar with the service-account email (**Make changes to events**).
4. In Discord: `/calendar link <calendar_id>` (the calendar's ID from its settings). New/edited/cancelled events now sync to Google. If unset, the internal Postgres calendar + iCal feed still work.

## iCal feed

`/calendar feed` returns `PUBLIC_BASE_URL/calendar/<guild_id>/<token>.ics`. Add it as a *subscribe-by-URL* calendar in Google/Apple/Outlook. Set `PUBLIC_BASE_URL` to a reachable https URL in production (put the bot behind a reverse proxy).

## Recording & consent

Scribe posts a visible "🔴 Recording started" notice when recording begins. Recording people's voices carries legal obligations in some jurisdictions — make sure participants consent. You are responsible for lawful use.

## Repo layout

```
packages/quorum_shared/   config, db models, gcal, transcription+minutes providers
apps/scheduler_bot/       events cog, APScheduler reminders, FastAPI iCal feed
apps/scribe_bot/          record cog, audio→transcript→minutes pipeline
docker-compose.yml        postgres + both bots (+ adminer under `--profile dev`)
```

## Notes / roadmap

- Schema is bootstrapped with `create_all`; add Alembic before production schema changes.
- Per-speaker transcription bills each track's full duration; a VAD-based per-utterance extraction (preserving offsets) would cut cost — good first contribution.
- Built on: [Pycord](https://pycord.dev) (MIT), [APScheduler](https://apscheduler.readthedocs.io) (MIT), Groq / DeepSeek / OpenRouter APIs, ffmpeg. Inspired by [Craig](https://craig.chat) for the per-track model.

## License

MIT — see [LICENSE](LICENSE).
