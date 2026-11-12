"""Settings and paths.

Settings live in Hermes' ``config.yaml`` under ``plugins.entries.hermes-somnia.settings``
(declared in ``plugin.yaml``'s ``config_schema``). Every key has a default, so the plugin
works with no configuration at all (destination ``builtin``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .hermes_api import PLUGIN_ID, hermes_home, load_config

# Settings the desktop page may change inline (everything else lives in the Plugins hub).
PAGE_SETTINGS = ("destructive", "max_promotions", "min_score", "min_sessions", "diary", "diary_language", "notify",
                 "principles")


@dataclass
class Settings:
    # where Deep writes: auto (the vault when one is configured, else builtin), builtin (MEMORY.md/USER.md),
    # vault (markdown second brain) or both
    destinations: str = "auto"
    schedule: str = "0 3 * * *"
    timezone: str = ""  # IANA name; empty = local time of the host
    # builtin sink
    targets: list = field(default_factory=lambda: ["memory", "user"])
    headroom: float = 0.10  # keep this fraction of each char budget free after a dream
    destructive: str = "apply"  # apply (OpenClaw parity; backup + undo) | stage (-> /memory pending)
    max_entry_chars: int = 300
    max_prior_entry_loss_fraction: float = 0.25
    # vault sink
    vault_path: str = ""  # else $OBSIDIAN_VAULT_PATH, else $WIKI_PATH
    vault_folder: str = "somnia"  # Somnia-owned inbox inside the vault
    vault_pointer: bool = True  # keep one short MEMORY.md entry pointing at the inbox
    protected_paths: list = field(default_factory=list)  # vault-relative paths never written
    vault_note_type: str = "inbox"  # frontmatter `type:` of the inbox notes (match your vault's schema)
    vault_note_tags: list = field(default_factory=lambda: ["somnia"])
    vault_language: str = "en"  # en | pt-BR: headings and labels of the inbox notes
    vault_sync_command: str = ""  # run after each vault write, e.g. a git commit+push; {note} {folder} {day}
    # ingestion
    ingest_days: int = 14
    max_messages: int = 600
    max_extract_calls: int = 12
    extract_chunk_chars: int = 20000
    interactive_sources: list = field(
        default_factory=lambda: ["cli", "tui", "desktop", "telegram", "discord", "whatsapp", "signal", "slack",
                                 "matrix", "email", "sms", "webui", "api"])
    # light
    light_lookback_days: int = 2
    light_limit: int = 100
    light_dedupe_similarity: float = 0.9
    # rem
    rem_lookback_days: int = 7
    rem_limit: int = 10
    rem_min_pattern_strength: float = 0.75
    # deep
    max_promotions: int = 3
    min_score: float = 0.65
    min_signals: int = 2
    min_sessions: int = 2
    min_durability: float = 0.5
    max_age_days: int = 30
    recency_half_life_days: int = 14
    # lifecycle
    stale_days: int = 45  # a Somnia entry not re-observed for this long is flagged stale (never auto-deleted)
    reask_days: int = 180  # a forgotten fact that keeps recurring is offered again after this long
    reask_min_recur: int = 3
    alarm_noop_nights: int = 3  # consecutive nights with material but no promotion before the alarm
    # diary + output
    diary: bool = True
    fact_language: str = "auto"  # auto = the language the user spoke; or e.g. English, pt-BR
    diary_language: str = "auto"  # auto = the language of your conversations; or e.g. English, pt-BR
    notify: str = "changes"  # changes | always | never (cron stdout)
    llm: bool = True
    mask_secrets: bool = True
    # thinking rules (see principles.py): pareto, lindy, inversion, occam, feynman
    principles: list = field(default_factory=lambda: ["pareto", "lindy", "occam", "feynman"])

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "Settings":
        s = cls()
        if not isinstance(raw, dict):
            return s
        for f in fields(cls):
            if f.name in raw and raw[f.name] is not None:
                default = getattr(s, f.name)
                value = raw[f.name]
                try:
                    if isinstance(default, bool):
                        value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                    elif isinstance(default, int):
                        value = int(value)
                    elif isinstance(default, float):
                        value = float(value)
                    elif isinstance(default, list):
                        value = [v.strip() for v in value.split(",")] if isinstance(value, str) else list(value)
                    else:
                        value = str(value)
                except (TypeError, ValueError):
                    continue
                setattr(s, f.name, value)
        return s

    @property
    def resolved_destinations(self) -> str:
        if self.destinations != "auto":
            return self.destinations
        vault = self.resolved_vault()
        return "vault" if vault is not None and vault.is_dir() else "builtin"

    @property
    def wants_builtin(self) -> bool:
        return self.resolved_destinations in ("builtin", "both")

    @property
    def wants_vault(self) -> bool:
        return self.resolved_destinations in ("vault", "both")

    def resolved_vault(self) -> Path | None:
        for candidate in (self.vault_path, os.environ.get("OBSIDIAN_VAULT_PATH"), os.environ.get("WIKI_PATH")):
            if candidate:
                return Path(os.path.expanduser(candidate)).resolve()
        return None


def load_settings() -> Settings:
    cfg = load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    return Settings.from_mapping(entry.get("settings") if isinstance(entry, dict) else None)


def state_dir() -> Path:
    override = os.environ.get("HERMES_SOMNIA_STATE_DIR")
    path = Path(override) if override else hermes_home() / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_db_path() -> Path:
    return Path(os.environ.get("HERMES_SOMNIA_SESSIONS_DB") or hermes_home() / "state.db")
