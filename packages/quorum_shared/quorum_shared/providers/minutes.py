"""Minutes generation: transcript text -> structured meeting minutes.

Default provider is DeepSeek (``deepseek-v4-flash``, 1M context, JSON mode),
driven through the OpenAI-compatible SDK. Selected by ``MINUTES_PROVIDER``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from ..config import settings

_SYSTEM_PROMPT = (
    "You are a meeting-minutes assistant. Read the transcript and return the "
    "minutes strictly as a json object with exactly these keys:\n"
    '  "summary": a concise prose summary of the meeting (string),\n'
    '  "decisions": a list of decisions made (array of strings),\n'
    '  "action_items": a list of objects, each {"owner": string, "task": string, '
    '"due": string} where "due" may be an empty string if unspecified.\n'
    "Respond with json only, no markdown fences. "
    'Example: {"summary":"The team agreed on the Q3 roadmap.",'
    '"decisions":["Ship v2 in August"],'
    '"action_items":[{"owner":"Alex","task":"Draft the migration plan","due":"Friday"}]}'
)


@dataclass
class MinutesResult:
    summary: str
    decisions: list[str] = field(default_factory=list)
    action_items: list[dict[str, str]] = field(default_factory=list)


class MinutesProvider(Protocol):
    def generate(self, transcript_text: str) -> MinutesResult:
        ...


def _parse_minutes(content: str) -> MinutesResult:
    """Parse the model's JSON output defensively."""
    content = content.strip()
    # Strip accidental markdown fences.
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return MinutesResult(summary=content or "(no summary returned)")

    action_items: list[dict[str, str]] = []
    for item in data.get("action_items") or []:
        if isinstance(item, dict):
            action_items.append(
                {
                    "owner": str(item.get("owner", "")).strip(),
                    "task": str(item.get("task", "")).strip(),
                    "due": str(item.get("due", "")).strip(),
                }
            )
        elif isinstance(item, str):
            action_items.append({"owner": "", "task": item.strip(), "due": ""})

    decisions = [str(d).strip() for d in (data.get("decisions") or []) if str(d).strip()]
    return MinutesResult(
        summary=str(data.get("summary", "")).strip() or "(no summary returned)",
        decisions=decisions,
        action_items=action_items,
    )


class DeepSeekMinutes:
    def __init__(self) -> None:
        if not settings.deepseek_api_key:
            raise RuntimeError("MINUTES_PROVIDER=deepseek but DEEPSEEK_API_KEY is empty.")
        from openai import OpenAI

        self._client = OpenAI(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        )
        self._model = settings.deepseek_model

    def generate(self, transcript_text: str) -> MinutesResult:
        resp = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript_text},
            ],
            max_tokens=4000,
            temperature=0.2,
        )
        return _parse_minutes(resp.choices[0].message.content or "")


class OpenRouterMinutes:
    """Fallback: any OpenRouter chat model that supports JSON output."""

    def __init__(self) -> None:
        if not settings.openrouter_api_key:
            raise RuntimeError("MINUTES_PROVIDER=openrouter but OPENROUTER_API_KEY is empty.")
        from openai import OpenAI

        self._client = OpenAI(
            api_key=settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1"
        )
        # Reuse the deepseek model slug via OpenRouter if desired.
        self._model = settings.deepseek_model

    def generate(self, transcript_text: str) -> MinutesResult:
        resp = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript_text},
            ],
            max_tokens=4000,
            temperature=0.2,
        )
        return _parse_minutes(resp.choices[0].message.content or "")


_PROVIDERS = {
    "deepseek": DeepSeekMinutes,
    "openrouter": OpenRouterMinutes,
}


def get_minutes_provider() -> MinutesProvider:
    key = settings.minutes_provider.lower().strip()
    try:
        return _PROVIDERS[key]()  # type: ignore[return-value]
    except KeyError:
        raise RuntimeError(
            f"Unknown MINUTES_PROVIDER '{key}'. Options: {', '.join(_PROVIDERS)}."
        ) from None
