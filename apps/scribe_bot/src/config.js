// Central configuration for the Scribe (Node) bot, read from the environment.
// Mirrors the keys the Python side uses so a single .env drives the whole suite.

// Keep values as STRINGS — Discord snowflake IDs exceed JS's safe integer
// range, so Number() silently rounds them to the wrong id.
function csvStrings(v) {
  if (!v) return [];
  return v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// SQLAlchemy uses a `postgresql+psycopg://` URL; node-postgres needs a plain
// `postgresql://` URL. Strip the driver suffix so both share one DATABASE_URL.
function normalizePgUrl(url) {
  if (!url) return url;
  return url.replace(/^postgresql\+\w+:\/\//, "postgresql://").replace(/^postgres\+\w+:\/\//, "postgres://");
}

export const config = {
  databaseUrl: normalizePgUrl(process.env.DATABASE_URL || ""),
  token: process.env.DISCORD_TOKEN_SCRIBE || "",
  devGuildIds: csvStrings(process.env.DEV_GUILD_IDS),

  transcribeProvider: (process.env.TRANSCRIBE_PROVIDER || "groq").toLowerCase(),
  groqApiKey: process.env.GROQ_API_KEY || "",
  groqModel: process.env.GROQ_MODEL || "whisper-large-v3-turbo",
  openrouterApiKey: process.env.OPENROUTER_API_KEY || "",
  openrouterTranscribeModel: process.env.OPENROUTER_TRANSCRIBE_MODEL || "openai/whisper-large-v3",

  minutesProvider: (process.env.MINUTES_PROVIDER || "deepseek").toLowerCase(),
  deepseekApiKey: process.env.DEEPSEEK_API_KEY || "",
  deepseekModel: process.env.DEEPSEEK_MODEL || "deepseek-v4-flash",
  deepseekBaseUrl: process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com",

  maxRecordingMinutes: Number(process.env.MAX_RECORDING_MINUTES || 180),
  chunkSeconds: 480, // ~15 MB per 16 kHz mono chunk; under Groq's 25 MB cap
};
