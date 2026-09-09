# Build context = repo root (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

# ffmpeg: chunk/re-encode audio. libopus0: Opus decode for voice receive (PyNaCl
# handles crypto but libopus must be loadable for decoding received audio).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY packages/quorum_shared /app/packages/quorum_shared
RUN pip install /app/packages/quorum_shared

COPY apps/scribe_bot/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Optional local transcription fallback:
#   RUN pip install "faster-whisper>=1.2.1"
# (leave off by default; the VPS is GPU-less and Groq is the default provider.)

COPY apps/scribe_bot /app/scribe_bot
WORKDIR /app/scribe_bot

CMD ["python", "bot.py"]
