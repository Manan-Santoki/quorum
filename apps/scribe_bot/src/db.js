// Postgres access for Scribe. Writes to the SAME tables the Python scheduler
// creates (recordings / transcripts / minutes), so the two bots share state.

import pg from "pg";
import { config } from "./config.js";

export const pool = new pg.Pool({ connectionString: config.databaseUrl });

// An event is "due" for auto-recording when now is within [start-10m, start+30m],
// i.e. its start_at is within [now-30m, now+10m]. Returns the soonest such event.
export async function findDueEvent(guildId) {
  const { rows } = await pool.query(
    `SELECT id, channel_id
       FROM events
      WHERE guild_id = $1
        AND cancelled = false
        AND start_at BETWEEN (now() - interval '30 minutes') AND (now() + interval '10 minutes')
      ORDER BY start_at
      LIMIT 1`,
    [guildId]
  );
  return rows[0] || null;
}

export async function eventBelongs(eventId, guildId) {
  const { rows } = await pool.query(
    "SELECT 1 FROM events WHERE id = $1 AND guild_id = $2",
    [eventId, guildId]
  );
  return rows.length > 0;
}

export async function createRecording({ guildId, channelId, voiceChannelId, startedBy, eventId }) {
  const { rows } = await pool.query(
    `INSERT INTO recordings
       (guild_id, channel_id, voice_channel_id, started_by, event_id, status, started_at)
     VALUES ($1, $2, $3, $4, $5, 'recording', now())
     RETURNING id`,
    [guildId, channelId, voiceChannelId, startedBy, eventId ?? null]
  );
  return rows[0].id;
}

export async function setStatus(recordingId, status, error = null) {
  const terminal = status === "complete" || status === "failed";
  await pool.query(
    `UPDATE recordings
       SET status = $2::recording_status,
           error = COALESCE($3, error),
           ended_at = CASE WHEN $4 THEN now() ELSE ended_at END
     WHERE id = $1`,
    [recordingId, status, error, terminal]
  );
}

export async function saveResults(recordingId, { transcript, summary, decisions, actionItems, postedMessageId }) {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      "INSERT INTO transcripts (recording_id, text) VALUES ($1, $2)",
      [recordingId, transcript]
    );
    await client.query(
      `INSERT INTO minutes
         (recording_id, summary, decisions_json, action_items_json, posted_message_id)
       VALUES ($1, $2, $3, $4, $5)`,
      [
        recordingId,
        summary,
        JSON.stringify(decisions ?? []),
        JSON.stringify(actionItems ?? []),
        postedMessageId ?? null,
      ]
    );
    await client.query(
      "UPDATE recordings SET status = 'complete', ended_at = now() WHERE id = $1",
      [recordingId]
    );
    await client.query("COMMIT");
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }
}
