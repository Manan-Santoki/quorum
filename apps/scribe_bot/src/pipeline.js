// PCM tracks -> transcript -> minutes.
// Each per-speaker PCM is re-encoded to 16 kHz mono and split into <=chunk
// pieces (keeps uploads under Groq's 25 MB cap); chunk timestamps are offset
// back onto the meeting clock, then all speakers' segments merge by start time.

import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import OpenAI from "openai";
import { config } from "./config.js";

function ffmpeg(args) {
  return new Promise((resolve, reject) => {
    const p = spawn("ffmpeg", ["-hide_banner", "-loglevel", "error", ...args]);
    let err = "";
    p.stderr.on("data", (d) => (err += d.toString()));
    p.on("error", reject);
    p.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code}: ${err}`))));
  });
}

async function segmentPcm(pcmPath, outDir) {
  fs.mkdirSync(outDir, { recursive: true });
  await ffmpeg([
    "-y",
    "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", pcmPath,
    "-ac", "1", "-ar", "16000",
    "-f", "segment", "-segment_time", String(config.chunkSeconds),
    path.join(outDir, "chunk_%04d.wav"),
  ]);
  return fs
    .readdirSync(outDir)
    .filter((f) => f.endsWith(".wav"))
    .sort()
    .map((f) => path.join(outDir, f));
}

function makeTranscriber() {
  if (config.transcribeProvider === "openrouter") {
    if (!config.openrouterApiKey) throw new Error("TRANSCRIBE_PROVIDER=openrouter but OPENROUTER_API_KEY is empty.");
    return {
      client: new OpenAI({ apiKey: config.openrouterApiKey, baseURL: "https://openrouter.ai/api/v1" }),
      model: config.openrouterTranscribeModel,
    };
  }
  if (!config.groqApiKey) throw new Error("TRANSCRIBE_PROVIDER=groq but GROQ_API_KEY is empty.");
  return {
    client: new OpenAI({ apiKey: config.groqApiKey, baseURL: "https://api.groq.com/openai/v1" }),
    model: config.groqModel,
  };
}

function fmtTs(seconds) {
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

const MINUTES_SYSTEM_PROMPT =
  "You are a meeting-minutes assistant. Read the transcript and return the minutes strictly as a " +
  'json object with exactly these keys: "summary" (string), "decisions" (array of strings), ' +
  '"action_items" (array of objects each {"owner","task","due"} where "due" may be empty). ' +
  "Respond with json only, no markdown fences. " +
  'Example: {"summary":"The team agreed on the roadmap.","decisions":["Ship v2 in August"],' +
  '"action_items":[{"owner":"Alex","task":"Draft migration plan","due":"Friday"}]}';

async function generateMinutes(transcript) {
  if (config.minutesProvider === "openrouter") {
    if (!config.openrouterApiKey) throw new Error("MINUTES_PROVIDER=openrouter but OPENROUTER_API_KEY is empty.");
  } else if (!config.deepseekApiKey) {
    throw new Error("MINUTES_PROVIDER=deepseek but DEEPSEEK_API_KEY is empty.");
  }
  const client =
    config.minutesProvider === "openrouter"
      ? new OpenAI({ apiKey: config.openrouterApiKey, baseURL: "https://openrouter.ai/api/v1" })
      : new OpenAI({ apiKey: config.deepseekApiKey, baseURL: config.deepseekBaseUrl });

  const resp = await client.chat.completions.create({
    model: config.deepseekModel,
    response_format: { type: "json_object" },
    max_tokens: 4000,
    temperature: 0.2,
    messages: [
      { role: "system", content: MINUTES_SYSTEM_PROMPT },
      { role: "user", content: transcript },
    ],
  });
  const raw = resp.choices?.[0]?.message?.content ?? "";
  let data = {};
  try {
    data = JSON.parse(raw.replace(/^```(json)?/i, "").replace(/```$/, "").trim());
  } catch {
    return { summary: raw || "(no summary returned)", decisions: [], actionItems: [] };
  }
  const actionItems = (data.action_items || []).map((it) =>
    typeof it === "string"
      ? { owner: "", task: it, due: "" }
      : { owner: String(it.owner || ""), task: String(it.task || ""), due: String(it.due || "") }
  );
  return {
    summary: String(data.summary || "(no summary returned)"),
    decisions: (data.decisions || []).map(String).filter(Boolean),
    actionItems,
  };
}

export async function runPipeline(tracks, workdir) {
  const { client, model } = makeTranscriber();
  const tagged = []; // { name, start, text }
  const warnings = [];
  const speakers = new Set();

  for (const track of tracks) {
    let chunks;
    try {
      chunks = await segmentPcm(track.pcmPath, path.join(workdir, `chunks_${track.userId}`));
    } catch (e) {
      warnings.push(`ffmpeg failed for ${track.name}: ${e.message}`);
      continue;
    }
    for (let i = 0; i < chunks.length; i++) {
      const offset = i * config.chunkSeconds;
      try {
        const resp = await client.audio.transcriptions.create({
          file: fs.createReadStream(chunks[i]),
          model,
          response_format: "verbose_json",
          temperature: 0,
        });
        const segs = resp.segments || (resp.text ? [{ start: 0, end: 0, text: resp.text }] : []);
        for (const s of segs) {
          const text = (s.text || "").trim();
          if (!text) continue;
          tagged.push({ name: track.name, start: Number(s.start || 0) + offset, text });
          speakers.add(track.userId);
        }
      } catch (e) {
        warnings.push(`transcription failed for ${track.name} chunk ${i}: ${e.message}`);
      }
    }
  }

  tagged.sort((a, b) => a.start - b.start);
  const transcript = tagged.map((t) => `[${fmtTs(t.start)}] ${t.name}: ${t.text}`).join("\n");

  if (!transcript.trim()) {
    return {
      transcript: "(no speech was transcribed)",
      minutes: { summary: "No audible speech was captured in this recording.", decisions: [], actionItems: [] },
      warnings,
      speakerCount: speakers.size,
    };
  }

  let minutes;
  try {
    minutes = await generateMinutes(transcript);
  } catch (e) {
    warnings.push(`minutes generation failed: ${e.message}`);
    minutes = { summary: "(minutes generation failed; transcript is attached)", decisions: [], actionItems: [] };
  }

  return { transcript, minutes, warnings, speakerCount: speakers.size };
}
