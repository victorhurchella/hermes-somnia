"""Settings and paths.

Settings live in Hermes' ``config.yaml`` under ``plugins.entries.hermes-dreaming.settings``
(declared in ``plugin.yaml``'s ``config_schema``). Every key has a default, so the plugin
works with no configuration at all (destination ``builtin``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

PLUGIN_ID = "hermes-dreaming"


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


@dataclass
class Settings:
    # where Deep writes: builtin (MEMORY.md/USER.md), vault (markdown second brain) or both
    destinations: str = "builtin"
    schedule: str = "0 3 * * *"
    timezone: str = ""  # IANA name; empty = local time of the host
    # builtin sink
    targets: list = field(default_factory=lambda: ["memory", "user"])
    headroom: float = 0.10  # keep this fraction of each char budget free after a dream
    destructive: str = "stage"  # stage (-> /memory pending) | apply
    max_entry_chars: int = 300
    max_prior_entry_loss_fraction: float = 0.25
    # vault sink
    vault_path: str = ""  # else $OBSIDIAN_VAULT_PATH, else $WIKI_PATH
    vault_folder: str = "dreams"  # dream-owned inbox inside the vault
    vault_pointer: bool = True  # keep one short MEMORY.md entry pointing at the inbox
    protected_paths: list = field(default_factory=list)  # vault-relative paths never written
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
    # diary + output
    diary: bool = True
    notify: str = "changes"  # changes | always | never (cron stdout)
    llm: bool = True

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
    def wants_builtin(self) -> bool:
        return self.destinations in ("builtin", "both")

    @property
    def wants_vault(self) -> bool:
        return self.destinations in ("vault", "both")

    def resolved_vault(self) -> Path | None:
        for candidate in (self.vault_path, os.environ.get("OBSIDIAN_VAULT_PATH"), os.environ.get("WIKI_PATH")):
            if candidate:
                return Path(os.path.expanduser(candidate)).resolve()
        return None


def load_settings() -> Settings:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
        return Settings.from_mapping(entry.get("settings"))
    except Exception:
        return Settings()


def state_dir() -> Path:
    override = os.environ.get("HERMES_DREAMING_STATE_DIR")
    path = Path(override) if override else hermes_home() / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_db_path() -> Path:
    return Path(os.environ.get("HERMES_DREAMING_SESSIONS_DB") or hermes_home() / "state.db")
