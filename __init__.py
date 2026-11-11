"""Somnia plugin entry point: ctx.llm binding, `hermes somnia` CLI, /somnia slash command,
desktop RPC methods, the somnia skill and the `auxiliary.somnia` model slot."""

from __future__ import annotations

import io
import logging
import os
import shlex
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

logger = logging.getLogger(__name__)
PLUGIN_DIR = Path(__file__).resolve().parent


def _slash(raw_args: str = "", **_ctx) -> str:
    from .hermes_somnia import cli
    from .hermes_somnia.config import state_dir

    args = shlex.split(raw_args or "") or ["status"]
    if args[0] == "run" and "--dry-run" not in args:
        log = state_dir() / "last-manual-run.log"
        with open(log, "w") as fh:
            subprocess.Popen([sys.executable, str(PLUGIN_DIR / "scripts" / "hermes-somnia.py"), "run", "--verbose",
                              *args[1:]], stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
                             env={**os.environ, "HERMES_SOMNIA_PLUGIN_DIR": str(PLUGIN_DIR)})
        return f"Somnia is dreaming in the background. Output: {log}\nCheck later with /somnia status."
    if args[0] not in ("status", "diary", "explain", "run", "doctor", "pin", "forget", "unforget"):
        return "Usage: /somnia [status | run [--dry-run] | diary [-n N] | explain <text> | doctor | pin|forget|unforget <key>]"
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            cli.main(args)
        except SystemExit:
            pass
    return buf.getvalue().strip() or "(no output)"


def register(ctx) -> None:
    from .hermes_somnia import cli, hermes_api, rpc

    hermes_api.bind_ctx(ctx)
    if getattr(ctx, "llm", None) is not None:
        hermes_api.bind_ctx_llm(ctx.llm)

    def setup(subparser):
        cli.build_parser(subparser)

    steps = [
        ("aux", lambda: ctx.register_auxiliary_task(
            "somnia", display_name="Somnia",
            description="Nightly memory consolidation: candidate extraction, memory curation, dream diary")),
        ("cli", lambda: ctx.register_cli_command("somnia", "Somnia: offline memory consolidation (Light, REM, Deep)",
                                                 setup, cli.dispatch)),
        ("slash", lambda: ctx.register_command("somnia", _slash, "Somnia memory dreaming: status, run, diary, explain",
                                               args_hint="[status|run|diary|explain|doctor]")),
        ("skill", lambda: ctx.register_skill("somnia", PLUGIN_DIR / "skills" / "somnia" / "SKILL.md",
                                             "How Somnia consolidates memory and how to review it")),
    ]
    if hasattr(ctx, "register_rpc"):
        for name, fn in rpc.HANDLERS.items():
            steps.append((f"rpc:{name}", lambda name=name, fn=fn: ctx.register_rpc(name, rpc.wrap(fn))))
    for name, call in steps:
        try:
            call()
        except Exception as exc:  # an older Hermes without one of these APIs still gets the rest
            logger.warning("hermes-somnia: could not register %s: %s", name, exc)
