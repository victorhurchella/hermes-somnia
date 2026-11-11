---
name: somnia
description: Use when asked about Somnia, dreaming, the Dream Diary or why something is remembered.
---

# Somnia (hermes-somnia plugin)

Somnia consolidates memory offline. A nightly job (`hermes-somnia`) runs as a script with no agent turn, in three phases:

1. **Light** reads new user turns and final replies from interactive sessions. It never reads tool output,
   delegation results, cron runs or secrets, which are masked. It extracts short candidate facts, each backed by a
   verbatim quote, and stages the fresh ones.
2. **REM** finds recurring themes and "possible lasting truths". It is rule-based and only reinforces ranking.
3. **Deep** scores candidates on six signals plus the principle bonuses. At most 3 per night pass every gate, and
   they are written to MEMORY.md / USER.md (directly, with a backup) or to the vault inbox. Procedures become skill
   ideas instead of memory.

Principles (switchable): **pareto** (under pressure, keep the vital few), **lindy** (protect long-confirmed facts),
**inversion** (corrections first; opt-in), **occam** (shortest true phrasing), **feynman** (entries stand alone).

## When the user asks
- What did you learn last night / is it working: `hermes somnia status`, or the Somnia page in the desktop app.
- Why do you remember X / why not: `hermes somnia explain "<words>"`. In the desktop app, answer with a paragraph
  that is only `::somnia-memory{key="<candidate key>"}` to show the evidence card.
- Forget something for good: `hermes somnia forget <key>`. Somnia will not learn it again.
- Make sure something is remembered: `hermes somnia pin <key>`.
- Revert the last write: `hermes somnia undo`. Preview a night: `hermes somnia run --dry-run -v`.
- Check the setup: `hermes somnia doctor`.

## Rules
- Never hand-edit MEMORY.md entries to "help" Somnia. It tracks its own entries by hash. Use the page or the CLI.
- The Dream Diary (DREAMS.md) is narrative for humans. It is never a source of facts.
- In vault mode, treat inbox notes as unreviewed. Move a fact into a curated note only when the user confirms.
