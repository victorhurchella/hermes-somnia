---
name: dreaming
description: Use when asked about dreaming, the Dream Diary or memory consolidation.
---

# Dreaming (hermes-dreaming plugin)

A nightly script-only cron job (`hermes-dreaming`) consolidates memory offline, across sessions, in three phases:

1. **Light** reads new user turns and final replies from interactive sessions (never tool output, delegation
   results or cron runs), extracts short candidate facts, and stages the fresh ones.
2. **REM** finds recurring themes and "possible lasting truths" among staged candidates. Rule-based, no LLM.
   It only reinforces the ranking.
3. **Deep** scores every candidate on six signals (frequency, relevance, session diversity, recency,
   consolidation across days, conceptual richness) plus the Light/REM boost. At most 3 per night pass every
   gate. They are then written:
   - `builtin`: merged into MEMORY.md / USER.md under the char budget. Merges and replacements wait in
     `/memory pending` unless `destructive: apply`.
   - `vault`: appended as checkboxes to `<vault>/<vault_folder>/YYYY-MM-DD.md` (status: inbox).

## When the user asks about it
- What happened last night: `hermes dreaming status`, or read the newest report in
  `$HERMES_HOME/plugin-data/hermes-dreaming/reports/`.
- Why something was (not) remembered: `hermes dreaming explain "<words from the fact>"`.
- Preview without writing: `hermes dreaming run --dry-run`.
- Revert the last builtin write: `hermes dreaming undo`.
- The Dream Diary (`DREAMS.md`, in the plugin data dir or the vault folder) is narrative for humans. Never use it
  as a source of facts.

## Rules
- Never edit `MEMORY.md` entries to "help" dreaming. Its own entries are tracked by hash so it can merge them.
- In vault mode, treat inbox notes as unreviewed. Move a fact into a curated note only when the user confirms it.
