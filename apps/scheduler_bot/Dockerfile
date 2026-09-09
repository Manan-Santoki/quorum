# Build context = repo root (see docker-compose.yml) so we can COPY the shared package.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# Shared library first (better layer caching).
COPY packages/quorum_shared /app/packages/quorum_shared
RUN pip install /app/packages/quorum_shared

# App deps.
COPY apps/scheduler_bot/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# App source (run from here so `reminders`, `util`, `cogs` import as top-level).
COPY apps/scheduler_bot /app/scheduler_bot
WORKDIR /app/scheduler_bot

CMD ["python", "bot.py"]
