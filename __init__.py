"""hermes-dreaming plugin entry point: CLI subcommand, /dreaming slash command, skill, aux task."""

from __future__ import annotations

import io
import logging
import shlex
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

logger = logging.getLogger(__name__)
PLUGIN_DIR = Path(__file__).resolve().parent


def _cli():
    from .hermes_dreaming import cli

    return cli


def _slash(raw_args: str = "", **_ctx) -> str:
    args = shlex.split(raw_args or "") or ["status"]
    cli = _cli()
    if args[0] == "run" and "--dry-run" not in args:
        from .hermes_dreaming.config import state_dir

        log = state_dir() / "last-manual-run.log"
        with open(log, "w") as fh:
            subprocess.Popen([sys.executable, str(PLUGIN_DIR / "scripts" / "hermes-dreaming.py"), "run", "--verbose",
                              *args[1:]], stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
                             env={**__import__("os").environ, "HERMES_DREAMING_PLUGIN_DIR": str(PLUGIN_DIR)})
        return f"Dreaming sweep started in the background. Output: {log}\nCheck later with /dreaming status."
    if args[0] not in ("status", "diary", "explain", "run"):
        return "Usage: /dreaming [status | run [--dry-run] | diary [-n N] | explain <text>]"
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            cli.main(args)
        except SystemExit:
            pass
    return buf.getvalue().strip() or "(no output)"


def register(ctx) -> None:
    cli = _cli()

    def setup(subparser):
        cli.build_parser(subparser)

    def handler(args):
        return cli.dispatch(args)

    for name, call in (
        ("cli", lambda: ctx.register_cli_command("dreaming", "Offline memory consolidation (Light, REM, Deep)",
                                                 setup, handler)),
        ("slash", lambda: ctx.register_command("dreaming", _slash, "Memory dreaming: status, run, diary, explain",
                                               args_hint="[status|run|diary|explain]")),
        ("skill", lambda: ctx.register_skill("dreaming", PLUGIN_DIR / "skills" / "dreaming" / "SKILL.md",
                                             "How hermes-dreaming consolidates memory and how to review it")),
        ("aux", lambda: ctx.register_auxiliary_task(
            "dreaming", display_name="Dreaming",
            description="Nightly memory consolidation: candidate extraction, memory curation, dream diary")),
    ):
        try:
            call()
        except Exception as exc:  # an older Hermes without one of these APIs still gets the rest
            logger.warning("hermes-dreaming: could not register %s: %s", name, exc)
