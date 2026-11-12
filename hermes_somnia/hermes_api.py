"""The only module that touches Hermes internals.

Everything Somnia needs from Hermes goes through here, so a Hermes release that moves an internal
breaks one adapter instead of the whole plugin, and every private dependency has a fallback:

| Need | Primary (public API first) | Fallback |
|---|---|---|
| LLM | ``ctx.llm`` / ``agent.plugin_llm.PluginLlm`` (public) | ``agent.auxiliary_client.call_llm`` |
| Secret masking | ``agent.redact.redact_sensitive_text`` (public) | built-in regexes |
| Config | ``hermes_cli.config.load_config`` (public) | none (defaults) |
| MEMORY.md / USER.md | ``tools.memory_tool`` MemoryStore (private: lock, atomic write, threat scan) | compatible writer below |
| Approval queue | ``tools.write_approval`` (private) | staging unavailable -> apply |
| Cron job | ``hermes cron`` CLI (public) | printed command |
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-somnia"
AUX_TASK = "somnia"
DELIM = "\n§\n"

_CTX_LLM: Any = None  # set by register(ctx) when running inside a Hermes process with the plugin loaded


def bind_ctx_llm(llm: Any) -> None:
    global _CTX_LLM
    _CTX_LLM = llm


# --- paths / config ---

def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def load_config() -> dict:
    try:
        from hermes_cli.config import load_config as _load

        return _load() or {}
    except Exception:
        return {}


_CTX: Any = None


def bind_ctx(ctx: Any) -> None:
    global _CTX
    _CTX = ctx


def save_plugin_setting(key: str, value: Any) -> bool:
    """``ctx.set_config`` (public) inside Hermes, else Hermes' settings writer."""
    try:
        if _CTX is not None and hasattr(_CTX, "set_config"):
            _CTX.set_config(key, value)
            return True
    except Exception as exc:
        logger.warning("somnia: ctx.set_config(%s) failed: %s", key, exc)
    try:
        from hermes_cli.plugins_state import _plugin_relative_segments, save_plugin_setting as _save

        _save(PLUGIN_ID, _plugin_relative_segments(key), value)
        return True
    except Exception as exc:
        logger.warning("somnia: could not save setting %s: %s", key, exc)
        return False


def hermes_cli_path() -> list[str]:
    exe = shutil.which("hermes")
    if exe:
        return [exe]
    for cand in (Path(sys.prefix).parent / "bin" / "hermes", Path("/opt/hermes/bin/hermes")):
        if cand.exists():
            return [str(cand)]
    return [sys.executable, "-m", "hermes_cli.main"]


# --- LLM ---

class LLMUnavailable(RuntimeError):
    pass


def _plugin_llm():
    if _CTX_LLM is not None:
        return _CTX_LLM
    from agent.plugin_llm import PluginLlm  # public plugin API

    return PluginLlm(plugin_id=PLUGIN_ID)


def llm_available() -> bool:
    try:
        _plugin_llm()
        return True
    except Exception:
        try:
            import agent.auxiliary_client  # noqa: F401

            return True
        except Exception:
            return False


def _aux_task_configured() -> bool:
    cfg = load_config()
    aux = cfg.get("auxiliary") if isinstance(cfg, dict) else None
    return isinstance(aux, dict) and isinstance(aux.get(AUX_TASK), dict) and bool(aux.get(AUX_TASK))


def complete(system: str, user: str, max_tokens: int = 2000) -> str:
    """One chat completion on the user's own Hermes provider. Inside Hermes (plugin loaded) it goes through
    ``ctx.llm`` and the plugin's ``auxiliary.somnia`` slot. In the nightly script process the plugin is not
    loaded, so the slot is not registered: an ``auxiliary.somnia`` block is then honoured through the
    auxiliary client, and without one the public ``PluginLlm`` runs on the main model."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if _CTX_LLM is None and _aux_task_configured():
        try:
            from agent.auxiliary_client import call_llm, extract_content_or_reasoning

            response = call_llm(task=AUX_TASK, messages=messages, max_tokens=max_tokens, temperature=0.2)
            return extract_content_or_reasoning(response) or ""
        except ImportError:
            pass
    try:
        llm = _plugin_llm()
    except Exception:
        llm = None
    if llm is not None:
        if _CTX_LLM is not None:
            try:
                res = llm.complete(messages, max_tokens=max_tokens, temperature=0.2, task=AUX_TASK, purpose="somnia")
                return getattr(res, "text", "") or ""
            except Exception as exc:
                if "trust" not in type(exc).__name__.lower() and "task" not in str(exc).lower():
                    raise
                logger.debug("somnia: auxiliary task routing unavailable (%s); using the main model", exc)
        res = llm.complete(messages, max_tokens=max_tokens, temperature=0.2, purpose="somnia")
        return getattr(res, "text", "") or ""
    try:
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning
    except Exception as exc:
        raise LLMUnavailable(f"no Hermes LLM access: {exc}") from exc
    response = call_llm(task=AUX_TASK, messages=messages, max_tokens=max_tokens, temperature=0.2)
    return extract_content_or_reasoning(response) or ""


def describe_llm() -> dict:
    cfg = load_config()
    aux = ((cfg.get("auxiliary") or {}).get(AUX_TASK) or {}) if isinstance(cfg, dict) else {}
    model = aux.get("model") or ((cfg.get("model") or {}).get("default") if isinstance(cfg.get("model"), dict)
                                 else cfg.get("model"))
    provider = aux.get("provider") or ((cfg.get("model") or {}).get("provider")
                                       if isinstance(cfg.get("model"), dict) else None)
    return {"provider": provider, "model": model, "task": AUX_TASK}


# --- secret masking ---

_SECRET_RES = [
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b(password|passwd|senha|secret|token|api[_-]?key)\s*[:=]\s*\S{4,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def mask_secrets(text: str) -> str:
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text  # public plugin API

        text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:
        pass
    for rx in _SECRET_RES:
        text = rx.sub("[REDACTED]", text)
    return text


# --- MEMORY.md / USER.md ---

_THREATS = [
    re.compile(r"(?i)ignore (all |any )?(previous|prior|above) (instructions|prompts)"),
    re.compile(r"(?i)(disregard|override) (the )?(system|developer) (prompt|instructions)"),
    re.compile(r"(?i)\b(curl|wget)\b[^\n]*\|\s*(sh|bash)"),
    re.compile(r"(?i)exfiltrat"),
    re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]"),
]


class FallbackMemoryStore:
    """Writes MEMORY.md/USER.md the way Hermes does (sibling ``.lock`` flock, re-read under the lock,
    atomic replace, all-or-nothing batch against the final budget, threat scan). Used only when
    ``tools.memory_tool`` cannot be imported."""

    def __init__(self, directory: Path, limits: dict[str, int]):
        self.dir = directory
        self.limits = limits

    def path(self, target: str) -> Path:
        return self.dir / ("USER.md" if target == "user" else "MEMORY.md")

    def read(self, target: str) -> list[str]:
        p = self.path(target)
        if not p.exists():
            return []
        raw = p.read_text(encoding="utf-8")
        return [e.strip() for e in raw.split(DELIM) if e.strip()]

    def apply_batch(self, target: str, ops: list[dict]) -> dict:
        for op in ops:
            content = op.get("content") or ""
            if op.get("action") == "add" and any(rx.search(content) for rx in _THREATS):
                return {"success": False, "error": "content blocked by the threat scan"}
        p = self.path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p.with_name(p.name + ".lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            working = self.read(target)
            for op in ops:
                act = op.get("action")
                if act == "add":
                    working.append((op.get("content") or "").strip())
                elif act == "remove":
                    old = (op.get("matched_entry") or op.get("old_text") or "").strip()
                    hits = [e for e in working if e == old] or [e for e in working if old and old in e]
                    if len(hits) != 1:
                        return {"success": False, "error": f"no unique entry matches {old[:60]!r}"}
                    working.remove(hits[0])
                elif act == "replace":
                    old = (op.get("old_text") or "").strip()
                    hits = [i for i, e in enumerate(working) if e == old]
                    if len(hits) != 1:
                        return {"success": False, "error": f"no unique entry matches {old[:60]!r}"}
                    working[hits[0]] = (op.get("content") or "").strip()
                else:
                    return {"success": False, "error": f"unsupported action {act!r}"}
            if len(DELIM.join(working)) > self.limits.get(target, 2200):
                return {"success": False, "error": "over the memory char limit"}
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(DELIM.join(working))
            os.replace(tmp, p)
        return {"success": True}


class HermesMemory:
    """MEMORY.md/USER.md through Hermes' own MemoryStore when available."""

    def __init__(self):
        self.native = None
        self._load = None
        try:
            from tools.memory_tool import load_on_disk_store

            self._load = load_on_disk_store
            self.native = load_on_disk_store()
        except Exception as exc:
            logger.info("somnia: Hermes MemoryStore unavailable (%s); using the compatible writer", exc)
            cfg = load_config().get("memory") or {}
            self.fallback = FallbackMemoryStore(hermes_home() / "memories", {
                "memory": int(cfg.get("memory_char_limit") or 2200), "user": int(cfg.get("user_char_limit") or 1375)})

    @property
    def native_store(self) -> bool:
        return self.native is not None

    def refresh(self) -> None:
        if self._load:
            self.native = self._load()

    def entries(self, target: str) -> list[str]:
        if self.native is not None:
            return list(self.native.user_entries if target == "user" else self.native.memory_entries)
        return self.fallback.read(target)

    def limit(self, target: str) -> int:
        if self.native is not None:
            return int(self.native.user_char_limit if target == "user" else self.native.memory_char_limit)
        return self.fallback.limits[target]

    def apply(self, target: str, ops: list[dict]) -> dict:
        if self.native is None:
            return self.fallback.apply_batch(target, ops)
        result = self.native.apply_batch(target, ops)
        self.refresh()
        return result

    def can_stage(self) -> bool:
        try:
            from tools import write_approval  # noqa: F401

            return self.native is not None
        except Exception:
            return False

    def stage(self, target: str, ops: list[dict], summary: str) -> dict:
        try:
            from tools import write_approval as wa
        except Exception:
            return {"success": False, "error": "Hermes approval queue unavailable"}
        if self.native is None:
            return {"success": False, "error": "Hermes approval queue unavailable"}
        check = self.native.resolve_batch_entries(target, ops)
        if not check.get("success"):
            return check
        payload: dict[str, Any] = {"action": "batch", "target": target, "operations": [
            op if m is None else {**op, "matched_entry": m} for op, m in zip(ops, check.get("matched_entries") or [])]}
        record = wa.stage_write(wa.MEMORY, payload, summary=summary[:200], origin="background_review")
        return {"success": True, "staged": True, "pending_id": record.get("id")}

    def pending(self) -> list[dict]:
        try:
            from tools import write_approval as wa

            return [r for r in wa.list_pending(wa.MEMORY) if "somnia" in (r.get("summary") or "").lower()]
        except Exception:
            return []

    def files(self) -> dict[str, Path]:
        if self.native is not None:
            try:
                from tools.memory_tool import get_memory_dir

                d = Path(get_memory_dir())
                return {"memory": d / "MEMORY.md", "user": d / "USER.md"}
            except Exception:
                pass
        d = hermes_home() / "memories"
        return {"memory": d / "MEMORY.md", "user": d / "USER.md"}


# --- cron (public CLI) ---

def _run_cli(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, "NO_COLOR": "1"}
    return subprocess.run(hermes_cli_path() + args, capture_output=True, text=True, timeout=timeout, env=env)


def set_core_config(key: str, value: str) -> bool:
    """Set one core config key through the public CLI (``hermes config set``), then read it back."""
    try:
        proc = _run_cli(["config", "set", key, value], 60)
    except Exception as exc:
        logger.warning("somnia: hermes config set %s failed: %s", key, exc)
        return False
    if proc.returncode != 0:
        logger.warning("somnia: hermes config set %s: %s", key, (proc.stderr or proc.stdout)[-300:])
        return False
    node: Any = load_config()
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return str(node) == value


def find_cron_job(name: str) -> Optional[dict]:
    jobs_file = hermes_home() / "cron" / "jobs.json"
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
        jobs = data.get("jobs") if isinstance(data, dict) else data
        for job in jobs or []:
            if isinstance(job, dict) and job.get("name") == name:
                return job
    except Exception:
        pass
    try:
        out = _run_cli(["cron", "list", "--all"], 30).stdout
    except Exception:
        return None
    match = re.search(r"([0-9a-f]{12}) \[(\w+)\]\s*\n\s*Name:\s*" + re.escape(name) + r"\b(?P<rest>(?:\n\s+\S.*)*)", out)
    if not match:
        return None
    nxt = re.search(r"Next run:\s*(\S+)", match.group("rest") or "")
    return {"id": match.group(1), "state": match.group(2), "next_run_at": nxt.group(1) if nxt else None}


def create_cron_job(name: str, schedule: str, script: str) -> tuple[bool, str]:
    args = ["cron", "create", schedule, "--name", name, "--script", script, "--no-agent", "--deliver", "local"]
    try:
        proc = _run_cli(args)
    except Exception as exc:
        return False, f"{exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()
