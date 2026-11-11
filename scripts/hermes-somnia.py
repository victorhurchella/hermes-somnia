#!/usr/bin/env python3
"""Cron entry point for Somnia (hermes-somnia).

`hermes somnia setup` copies this file into $HERMES_HOME/scripts/ (Hermes only runs cron
scripts from there) and creates a script-only job (`--no-agent`): no agent turn, no tokens
except Somnia's own LLM calls. Empty stdout = silent run.
"""

import os
import sys
from pathlib import Path


def _plugin_dir() -> Path:
    explicit = os.environ.get("HERMES_SOMNIA_PLUGIN_DIR")
    if explicit:
        return Path(explicit)
    here = Path(__file__).resolve().parent.parent
    if (here / "hermes_somnia").is_dir():  # running from a checkout
        return here
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugins" / "hermes-somnia"


sys.path.insert(0, str(_plugin_dir()))

from hermes_somnia.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["run", "--cron"]))
