# Build context = repo root (see docker-compose.yml).
FROM node:20-bookworm-slim

ENV NODE_ENV=production

# ffmpeg: re-encode/chunk audio. build tools: compile any native deps
# (@snazzah/davey for DAVE decryption may build a native addon).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/scribe_bot

# Install deps first for better layer caching.
COPY apps/scribe_bot/package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# App source.
COPY apps/scribe_bot/src ./src

CMD ["node", "src/index.js"]
