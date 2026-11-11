"""LLM plumbing: the ``complete(system, user, max_tokens) -> str`` callable the engine uses, and a
tolerant JSON parser. The real call goes through :mod:`hermes_somnia.hermes_api` (``ctx.llm``, the
user's own Hermes provider; ``auxiliary.somnia`` picks a cheaper model). Tests inject a fake."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from . import hermes_api

Complete = Callable[[str, str, int], str]
LLMUnavailable = hermes_api.LLMUnavailable


def default_complete() -> Optional[Complete]:
    return hermes_api.complete if hermes_api.llm_available() else None


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
