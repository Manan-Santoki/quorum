// Voice capture via @discordjs/voice (decrypts DAVE E2EE audio through
// @snazzah/davey, which @discordjs/voice loads automatically when installed).
//
// We record one continuous PCM track per speaker. discord.js hands us audio
// only while a user is speaking, so at each speaking burst we pad the gap since
// recording start with silence — this keeps every speaker's track aligned to a
// common meeting clock, so the merged transcript stays correctly ordered.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import prism from "prism-media";
import {
  joinVoiceChannel,
  entersState,
  EndBehaviorType,
  VoiceConnectionStatus,
} from "@discordjs/voice";

// 48 kHz, stereo, signed 16-bit LE == what the Opus decoder emits.
const BYTES_PER_MS = (48000 * 2 * 2) / 1000; // 192

export class RecordingSession {
  constructor(guild, voiceChannel) {
    this.guild = guild;
    this.voiceChannel = voiceChannel;
    this.workdir = fs.mkdtempSync(path.join(os.tmpdir(), "quorum-rec-"));
    this.users = new Map(); // userId -> { stream, writtenBytes, active, path }
    this.startMs = null;
    this.connection = null;
    this._speakingHandler = null;
  }

  async start() {
    this.connection = joinVoiceChannel({
      channelId: this.voiceChannel.id,
      guildId: this.guild.id,
      adapterCreator: this.guild.voiceAdapterCreator,
      selfDeaf: false, // must NOT be deafened to receive audio
      selfMute: true,
    });
    await entersState(this.connection, VoiceConnectionStatus.Ready, 20_000);
    this.startMs = Date.now();

    const receiver = this.connection.receiver;
    this._speakingHandler = (userId) => this._onSpeaking(receiver, userId);
    receiver.speaking.on("start", this._speakingHandler);
  }

  _ensureUser(userId) {
    let u = this.users.get(userId);
    if (!u) {
      const p = path.join(this.workdir, `${userId}.pcm`);
      u = { stream: fs.createWriteStream(p), writtenBytes: 0, active: false, path: p };
      this.users.set(userId, u);
    }
    return u;
  }

  _onSpeaking(receiver, userId) {
    const u = this._ensureUser(userId);
    if (u.active) return; // already subscribed for this burst
    u.active = true;

    // Pad the silent gap since recording start so tracks stay time-aligned.
    const nowMs = Date.now() - this.startMs;
    const haveMs = u.writtenBytes / BYTES_PER_MS;
    const gapMs = Math.max(0, Math.floor(nowMs - haveMs));
    if (gapMs > 0) {
      const silence = Buffer.alloc(gapMs * BYTES_PER_MS);
      u.stream.write(silence);
      u.writtenBytes += silence.length;
    }

    const opus = receiver.subscribe(userId, {
      end: { behavior: EndBehaviorType.AfterSilence, duration: 1000 },
    });
    const decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
    const pcm = opus.pipe(decoder);

    pcm.on("data", (chunk) => {
      u.stream.write(chunk);
      u.writtenBytes += chunk.length;
    });
    const done = () => {
      u.active = false;
    };
    pcm.on("end", done);
    pcm.on("error", done);
    opus.on("error", done);
  }

  async _displayName(userId) {
    let m = this.guild.members.cache.get(userId);
    if (!m) m = await this.guild.members.fetch(userId).catch(() => null);
    return m ? m.displayName || m.user.username : `User-${userId}`;
  }

  /** Stop capture, flush files, and return { workdir, tracks:[{userId,name,pcmPath}] }. */
  async stop() {
    if (this._speakingHandler && this.connection) {
      this.connection.receiver.speaking.off("start", this._speakingHandler);
    }
    try {
      this.connection?.destroy();
    } catch {
      /* ignore */
    }

    const tracks = [];
    const flushes = [];
    for (const [userId, u] of this.users) {
      flushes.push(new Promise((res) => u.stream.on("finish", res)));
      u.stream.end();
      tracks.push({ userId, pcmPath: u.path });
    }
    await Promise.all(flushes);

    for (const t of tracks) t.name = await this._displayName(t.userId);
    // Drop empty tracks (users who never actually produced audio).
    const nonEmpty = tracks.filter((t) => {
      try {
        return fs.statSync(t.pcmPath).size > 0;
      } catch {
        return false;
      }
    });
    return { workdir: this.workdir, tracks: nonEmpty };
  }
}
