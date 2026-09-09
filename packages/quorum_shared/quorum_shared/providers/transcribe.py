"""Transcription providers.

A ``Transcriber`` turns one audio file into time-stamped text segments. Three
implementations are provided; the active one is chosen by
``TRANSCRIBE_PROVIDER`` (groq | openrouter | faster_whisper).

Groq and OpenRouter are OpenAI-compatible HTTP APIs, so we drive both with the
``openai`` SDK by swapping ``base_url``. faster-whisper runs locally (CPU by
default on a GPU-less VPS) and is the offline fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import settings


@dataclass
class Segment:
    """A single transcribed span of speech (seconds relative to file start)."""

    start: float
    end: float
    text: str


class Transcriber(Protocol):
    def transcribe(self, audio_path: str, language: str | None = None) -> list[Segment]:
        ...


def _segments_from_verbose_json(data: object) -> list[Segment]:
    """Extract segments from an OpenAI-style ``verbose_json`` transcription."""
    segments: list[Segment] = []
    raw = getattr(data, "segments", None)
    if raw:
        for s in raw:
            # SDK objects expose attributes; dicts expose keys.
            get = (lambda k: getattr(s, k, None)) if not isinstance(s, dict) else s.get
            segments.append(
                Segment(
                    start=float(get("start") or 0.0),
                    end=float(get("end") or 0.0),
                    text=(get("text") or "").strip(),
                )
            )
    if not segments:
        # No segment granularity returned; fall back to one blob.
        text = getattr(data, "text", "") or ""
        if text.strip():
            segments.append(Segment(start=0.0, end=0.0, text=text.strip()))
    return segments


class _OpenAICompatTranscriber:
    """Shared implementation for Groq and OpenRouter (OpenAI audio API)."""

    def __init__(self, api_key: str, base_url: str, model: str, provider: str):
        if not api_key:
            raise RuntimeError(f"{provider} transcription selected but its API key is empty.")
        from openai import OpenAI  # local import keeps import-time light

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._provider = provider

    def transcribe(self, audio_path: str, language: str | None = None) -> list[Segment]:
        with open(audio_path, "rb") as fh:
            resp = self._client.audio.transcriptions.create(
                model=self._model,
                file=fh,
                response_format="verbose_json",
                language=language or None,
                temperature=0.0,
            )
        return _segments_from_verbose_json(resp)


class GroqTranscriber(_OpenAICompatTranscriber):
    def __init__(self) -> None:
        super().__init__(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            model=settings.groq_model,
            provider="Groq",
        )


class OpenRouterTranscriber(_OpenAICompatTranscriber):
    def __init__(self) -> None:
        super().__init__(
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            model=settings.openrouter_transcribe_model,
            provider="OpenRouter",
        )


class FasterWhisperTranscriber:
    """Local transcription via faster-whisper (CTranslate2).

    On a GPU-less VPS this defaults to a distil/small model and int8 compute.
    """

    def __init__(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "TRANSCRIBE_PROVIDER=faster_whisper but faster-whisper is not installed. "
                "Install the 'faster-whisper' extra of quorum-shared."
            ) from exc

        self._model = WhisperModel(
            settings.faster_whisper_model, device="cpu", compute_type="int8"
        )

    def transcribe(self, audio_path: str, language: str | None = None) -> list[Segment]:
        segments, _info = self._model.transcribe(
            audio_path, beam_size=5, vad_filter=True, language=language or None
        )
        return [
            Segment(start=float(s.start), end=float(s.end), text=(s.text or "").strip())
            for s in segments
        ]


_PROVIDERS = {
    "groq": GroqTranscriber,
    "openrouter": OpenRouterTranscriber,
    "faster_whisper": FasterWhisperTranscriber,
}


def get_transcriber() -> Transcriber:
    """Instantiate the transcriber selected by ``TRANSCRIBE_PROVIDER``."""
    key = settings.transcribe_provider.lower().strip()
    try:
        return _PROVIDERS[key]()  # type: ignore[return-value]
    except KeyError:
        raise RuntimeError(
            f"Unknown TRANSCRIBE_PROVIDER '{key}'. Options: {', '.join(_PROVIDERS)}."
        ) from None
