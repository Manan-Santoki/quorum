"""Pluggable AI providers: transcription and minutes generation."""

from .minutes import MinutesResult, get_minutes_provider
from .transcribe import Segment, get_transcriber

__all__ = ["Segment", "get_transcriber", "MinutesResult", "get_minutes_provider"]
