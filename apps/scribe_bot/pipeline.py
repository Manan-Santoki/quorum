"""Audio -> transcript -> minutes pipeline for Quorum Scribe.

Pycord's WaveSink hands us one full-length WAV per speaker, all aligned to the
meeting start. We:
  1. Re-encode each track to 16 kHz mono and split it into <=CHUNK_SECONDS
     pieces (keeps every upload under Groq's 25 MB/file cap).
  2. Transcribe each chunk, offsetting its segment timestamps by
     chunk_index * CHUNK_SECONDS so timings stay aligned to the meeting clock.
  3. Merge all speakers' segments by start time into one chronological
     transcript ("[MM:SS] Name: text").
  4. Summarise the transcript into structured minutes.

We deliberately do NOT silence-trim: that would shift per-speaker timestamps
and desync the cross-speaker merge. Chunking keeps us within provider limits
while preserving alignment.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from quorum_shared.config import settings
from quorum_shared.providers import Segment, get_minutes_provider, get_transcriber
from quorum_shared.providers.minutes import MinutesResult

log = logging.getLogger("quorum.pipeline")

CHUNK_SECONDS = 480  # ~15 MB per chunk at 16 kHz mono 16-bit; safely under 25 MB


@dataclass
class Track:
    user_id: int
    name: str
    wav_path: str  # raw WAV written from the sink


@dataclass
class PipelineResult:
    transcript: str
    minutes: MinutesResult
    warnings: list[str]
    speaker_count: int


def _ffmpeg_segment(raw_path: str, out_dir: str) -> list[str]:
    """Re-encode to 16 kHz mono and split into CHUNK_SECONDS WAV chunks."""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "chunk_%04d.wav")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", raw_path,
        "-ac", "1", "-ar", "16000",
        "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
        pattern,
    ]
    subprocess.run(cmd, check=True)
    return sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".wav")
    )


def _transcribe_track(track: Track, transcriber, workdir: str) -> tuple[list[tuple[str, Segment]], list[str]]:
    tagged: list[tuple[str, Segment]] = []
    warnings: list[str] = []
    chunk_dir = os.path.join(workdir, f"chunks_{track.user_id}")
    try:
        chunks = _ffmpeg_segment(track.wav_path, chunk_dir)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        warnings.append(f"ffmpeg failed for {track.name}: {exc}")
        return tagged, warnings

    for idx, chunk in enumerate(chunks):
        offset = idx * CHUNK_SECONDS
        try:
            segments = transcriber.transcribe(chunk)
        except Exception as exc:  # network / provider error, per-chunk
            warnings.append(f"transcription failed for {track.name} chunk {idx}: {exc}")
            continue
        for seg in segments:
            if not seg.text:
                continue
            tagged.append(
                (track.name, Segment(start=seg.start + offset, end=seg.end + offset, text=seg.text))
            )
    return tagged, warnings


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def build_transcript(tagged: list[tuple[str, Segment]]) -> str:
    tagged.sort(key=lambda t: t[1].start)
    return "\n".join(f"[{_fmt_ts(seg.start)}] {name}: {seg.text}" for name, seg in tagged)


def run_pipeline(tracks: list[Track], workdir: str) -> PipelineResult:
    """Blocking: transcribe every track, merge, and summarise. Run in a thread."""
    transcriber = get_transcriber()
    all_tagged: list[tuple[str, Segment]] = []
    warnings: list[str] = []
    speakers: set[int] = set()

    for track in tracks:
        tagged, warns = _transcribe_track(track, transcriber, workdir)
        all_tagged.extend(tagged)
        warnings.extend(warns)
        if tagged:
            speakers.add(track.user_id)

    transcript = build_transcript(all_tagged)
    if not transcript.strip():
        return PipelineResult(
            transcript="(no speech was transcribed)",
            minutes=MinutesResult(summary="No audible speech was captured in this recording."),
            warnings=warnings,
            speaker_count=len(speakers),
        )

    try:
        minutes = get_minutes_provider().generate(transcript)
    except Exception as exc:
        warnings.append(f"minutes generation failed: {exc}")
        minutes = MinutesResult(summary="(minutes generation failed; transcript is attached)")

    return PipelineResult(
        transcript=transcript, minutes=minutes, warnings=warnings, speaker_count=len(speakers)
    )
