// Quorum Scribe (Node) — records Discord voice under DAVE and posts AI minutes.

import {
  Client,
  GatewayIntentBits,
  Events,
  SlashCommandBuilder,
  EmbedBuilder,
  AttachmentBuilder,
  ChannelType,
  MessageFlags,
} from "discord.js";
import { config } from "./config.js";
import { RecordingSession } from "./recorder.js";
import { runPipeline } from "./pipeline.js";
import { createRecording, setStatus, saveResults, eventBelongs, findDueEvent } from "./db.js";

if (!config.token) {
  console.error("DISCORD_TOKEN_SCRIBE is not set.");
  process.exit(1);
}

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildVoiceStates,
    GatewayIntentBits.GuildMembers, // resolve speaker display names (privileged)
  ],
});

// guildId -> { session, recordingId, eventId, textChannelId, autostop }
const sessions = new Map();

const recordCommand = new SlashCommandBuilder()
  .setName("record")
  .setDescription("Record a voice meeting and get AI minutes")
  .addSubcommand((sc) =>
    sc
      .setName("start")
      .setDescription("Start recording (auto-joins a voice channel that has people in it)")
      .addIntegerOption((o) =>
        o.setName("event_id").setDescription("Link this recording to an event #").setRequired(false)
      )
  )
  .addSubcommand((sc) => sc.setName("stop").setDescription("Stop recording and generate minutes"));

async function registerCommands() {
  const body = [recordCommand.toJSON()];
  await client.application.commands.set(body); // global => works in every server
  // Clear any leftover guild-scoped commands from earlier dev registration.
  for (const gid of config.devGuildIds) {
    const g = await client.guilds.fetch(gid).catch(() => null);
    if (g) await g.commands.set([]).catch(() => {});
  }
  console.log("Registered global commands");
}

function pickVoiceChannel(interaction) {
  if (interaction.member?.voice?.channel) return interaction.member.voice.channel;
  let best = null;
  let bestCount = 0;
  for (const ch of interaction.guild.channels.cache.values()) {
    if (ch.type !== ChannelType.GuildVoice && ch.type !== ChannelType.GuildStageVoice) continue;
    const humans = ch.members.filter((m) => !m.user.bot).size;
    if (humans > bestCount) {
      best = ch;
      bestCount = humans;
    }
  }
  return best;
}

// Shared by manual /record start and automatic (event-triggered) recording.
async function startSession({ guild, voiceChannel, textChannel, startedBy, eventId, startedByMention }) {
  const session = new RecordingSession(guild, voiceChannel);
  await session.start();

  const recordingId = await createRecording({
    guildId: guild.id,
    channelId: textChannel.id,
    voiceChannelId: voiceChannel.id,
    startedBy,
    eventId,
  });

  const autostop = setTimeout(() => {
    console.log(`Auto-stopping recording in guild ${guild.id} (max duration).`);
    finishRecording(guild.id).catch((e) => console.error(e));
  }, config.maxRecordingMinutes * 60_000);

  sessions.set(guild.id, {
    session,
    recordingId,
    eventId,
    voiceChannelId: voiceChannel.id,
    textChannel,
    autostop,
    emptyTimer: null,
  });

  const who = startedByMention ? ` by ${startedByMention}` : " automatically for a scheduled event";
  await textChannel
    .send(
      `🔴 **Recording started** in ${voiceChannel.toString()}${who}. ` +
        "Everyone in the channel is being recorded for meeting minutes."
    )
    .catch(() => {});
  return { recordingId };
}

async function handleStart(interaction) {
  const guildId = interaction.guild.id;
  if (sessions.has(guildId)) {
    return interaction.reply({ content: "Already recording in this server. Use `/record stop`.", flags: MessageFlags.Ephemeral });
  }
  const voiceChannel = pickVoiceChannel(interaction);
  if (!voiceChannel) {
    return interaction.reply({
      content: "No one is in a voice channel. At least one person must join a voice channel first.",
      flags: MessageFlags.Ephemeral,
    });
  }
  const eventId = interaction.options.getInteger("event_id");
  await interaction.deferReply({ flags: MessageFlags.Ephemeral });

  if (eventId != null && !(await eventBelongs(eventId, guildId))) {
    return interaction.editReply(`No event #${eventId} in this server.`);
  }

  try {
    const { recordingId } = await startSession({
      guild: interaction.guild,
      voiceChannel,
      textChannel: interaction.channel,
      startedBy: interaction.user.id,
      eventId,
      startedByMention: interaction.user.toString(),
    });
    return interaction.editReply(`Recording started (recording #${recordingId}).`);
  } catch (e) {
    console.error("failed to start recording:", e);
    return interaction.editReply(`Couldn't start recording: \`${e.message}\``);
  }
}

// --- Auto-record: watch voice joins & channel-empty --------------------------
const autoStarted = new Set(); // event ids already auto-recorded this process

async function maybeAutoStart(guild, voiceChannel) {
  if (!voiceChannel || sessions.has(guild.id)) return;
  const ev = await findDueEvent(guild.id);
  if (!ev || autoStarted.has(ev.id)) return;
  autoStarted.add(ev.id);
  const textChannel = await client.channels.fetch(String(ev.channel_id)).catch(() => null);
  if (!textChannel) {
    autoStarted.delete(ev.id);
    return;
  }
  try {
    await startSession({
      guild,
      voiceChannel,
      textChannel,
      startedBy: client.user.id,
      eventId: ev.id,
    });
    console.log(`Auto-started recording for event ${ev.id} in guild ${guild.id}`);
  } catch (e) {
    console.error("auto-start failed:", e);
    autoStarted.delete(ev.id);
  }
}

function checkAutoStop(guild) {
  const s = sessions.get(guild.id);
  if (!s) return;
  const vc = guild.channels.cache.get(s.voiceChannelId);
  const humans = vc ? vc.members.filter((m) => !m.user.bot).size : 0;
  if (humans === 0) {
    if (!s.emptyTimer) {
      s.emptyTimer = setTimeout(() => {
        console.log(`Voice channel empty; stopping recording in guild ${guild.id}`);
        finishRecording(guild.id).catch((e) => console.error(e));
      }, 120_000); // 2 min grace after the last person leaves
    }
  } else if (s.emptyTimer) {
    clearTimeout(s.emptyTimer);
    s.emptyTimer = null;
  }
}

async function handleStop(interaction) {
  const guildId = interaction.guild.id;
  if (!sessions.has(guildId)) {
    return interaction.reply({ content: "I'm not recording in this server.", flags: MessageFlags.Ephemeral });
  }
  await interaction.deferReply({ flags: MessageFlags.Ephemeral });
  await interaction.editReply("⏹️ Stopped. Transcribing and writing minutes… this can take a bit.");
  await finishRecording(guildId);
}

async function finishRecording(guildId) {
  const s = sessions.get(guildId);
  if (!s) return;
  sessions.delete(guildId);
  clearTimeout(s.autostop);
  if (s.emptyTimer) clearTimeout(s.emptyTimer);

  const channel = s.textChannel;
  const { recordingId } = s;
  let stopped;
  try {
    stopped = await s.session.stop();
  } catch (e) {
    console.error("stop failed:", e);
    await setStatus(recordingId, "failed", String(e.message)).catch(() => {});
    await channel.send(`❌ Failed to finalize the recording: \`${e.message}\``).catch(() => {});
    return;
  }

  if (!stopped.tracks.length) {
    await setStatus(recordingId, "failed", "no audio").catch(() => {});
    await channel.send("⚠️ Recording finished but no audio was captured.").catch(() => {});
    return;
  }

  await setStatus(recordingId, "processing").catch(() => {});

  let result;
  try {
    result = await runPipeline(stopped.tracks, stopped.workdir);
  } catch (e) {
    console.error("pipeline failed:", e);
    await setStatus(recordingId, "failed", String(e.message)).catch(() => {});
    await channel.send(`❌ Failed to process the recording: \`${e.message}\``).catch(() => {});
    return;
  }

  const m = result.minutes;
  const embed = new EmbedBuilder()
    .setTitle("📝 Meeting Minutes")
    .setDescription(m.summary.slice(0, 4000))
    .setColor(0x2ecc71)
    .setFooter({ text: `Recording #${recordingId} • ${result.speakerCount} speaker(s)` });
  if (m.decisions.length) {
    embed.addFields({ name: "Decisions", value: m.decisions.map((d) => `• ${d}`).join("\n").slice(0, 1024) });
  }
  if (m.actionItems.length) {
    const lines = m.actionItems.map((a) => {
      const owner = a.owner ? `**${a.owner}** ` : "";
      const due = a.due ? ` _(due ${a.due})_` : "";
      return `• ${owner}${a.task}${due}`;
    });
    embed.addFields({ name: "Action Items", value: lines.join("\n").slice(0, 1024) });
  }

  const file = new AttachmentBuilder(Buffer.from(result.transcript, "utf-8"), {
    name: `transcript_${recordingId}.txt`,
  });

  let postedId = null;
  try {
    const msg = await channel.send({ embeds: [embed], files: [file] });
    postedId = msg.id;
    if (result.warnings.length) {
      await channel.send("⚠️ " + result.warnings.slice(0, 5).join("; ")).catch(() => {});
    }
  } catch (e) {
    console.error("failed to post minutes:", e);
  }

  await saveResults(recordingId, {
    transcript: result.transcript,
    summary: m.summary,
    decisions: m.decisions,
    actionItems: m.actionItems,
    postedMessageId: postedId,
  }).catch((e) => console.error("saveResults failed:", e));
}

client.once(Events.ClientReady, async (c) => {
  console.log(`Scribe logged in as ${c.user.tag} (id=${c.user.id})`);
  try {
    await registerCommands();
  } catch (e) {
    console.error("command registration failed:", e);
  }
});

client.on(Events.InteractionCreate, async (interaction) => {
  if (!interaction.isChatInputCommand() || interaction.commandName !== "record") return;
  const sub = interaction.options.getSubcommand();
  try {
    if (sub === "start") await handleStart(interaction);
    else if (sub === "stop") await handleStop(interaction);
  } catch (e) {
    console.error("command error:", e);
    const msg = `Something went wrong: \`${e.message}\``;
    if (interaction.deferred || interaction.replied) await interaction.editReply(msg).catch(() => {});
    else await interaction.reply({ content: msg, flags: MessageFlags.Ephemeral }).catch(() => {});
  }
});

client.on(Events.VoiceStateUpdate, async (oldState, newState) => {
  try {
    const guild = newState.guild || oldState.guild;
    const member = newState.member || oldState.member;
    // A human joined (or moved into) a voice channel → maybe auto-record.
    if (member && !member.user.bot && newState.channelId && oldState.channelId !== newState.channelId) {
      await maybeAutoStart(guild, newState.channel);
    }
    // Any change may have emptied an active recording's channel → maybe auto-stop.
    checkAutoStop(guild);
  } catch (e) {
    console.error("voiceStateUpdate error:", e);
  }
});

process.on("unhandledRejection", (e) => console.error("unhandledRejection:", e));

client.login(config.token);
