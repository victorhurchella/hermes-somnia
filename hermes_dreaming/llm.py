"""LLM access through Hermes' auxiliary client, with the user's own provider and credentials.

``call_llm(task="dreaming")`` honours an ``auxiliary.dreaming`` block in config.yaml (pick a cheap
model there); otherwise it falls back to Hermes' default auxiliary routing. Every caller in the
engine takes a plain ``complete(system, user, max_tokens) -> str`` callable, so tests inject a fake.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

Complete = Callable[[str, str, int], str]


class LLMUnavailable(RuntimeError):
    pass


def hermes_complete(system: str, user: str, max_tokens: int = 2000) -> str:
    try:
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning
    except Exception as exc:  # not running inside Hermes' Python
        raise LLMUnavailable(f"Hermes auxiliary client not importable: {exc}") from exc
    response = call_llm(
        task="dreaming",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.2,
    )
    return extract_content_or_reasoning(response) or ""


def default_complete() -> Optional[Complete]:
    try:
        import agent.auxiliary_client  # noqa: F401
    except Exception:
        return None
    return hermes_complete


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_json_object(text: str) -> Optional[dict[str, Any]]:
    """First JSON object in ``text`` (tolerates code fences and leading prose)."""
    if not text:
        return None
    raw = _FENCE_RE.sub("", text.strip())
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start = raw.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(raw[start:i + 1])
                        return value if isinstance(value, dict) else None
                    except ValueError:
                        break
        start = raw.find("{", start + 1)
    return None
