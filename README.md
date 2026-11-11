# hermes-dreaming

**OpenClaw's Dreaming, for Hermes Agent.** A nightly sweep reads your recent conversations, works out which
facts keep coming back, and consolidates them into long-term memory. It also keeps a Dream Diary you can read
over coffee.

```
            nightly cron (script-only, no agent turn)
                            │
   ┌────────────────────────┼─────────────────────────┐
   ▼                        ▼                         ▼
 LIGHT                     REM                      DEEP
 new turns → candidate     recurring themes,        6-signal score + gates →
 facts, deduped, staged    "possible lasting        curate MEMORY.md / USER.md
                           truths" (no LLM)         or write to your vault inbox
                            │
                            ▼
                  DREAMS.md (Dream Diary + Deep Sleep summary)
```

Hermes already saves memory during conversations: a background review every few turns can add an entry.
What it doesn't do is step back and look **across sessions**. It never notices that you've said the same thing
three times this week, never merges duplicates, never replaces a stale fact, and leaves no trail you can review.
OpenClaw's memory-core does this offline as "dreaming". This plugin ports the same three-phase design to Hermes,
with the same scoring math, diary prompt and safety rails, and adapts it to how Hermes stores memory.

## Works alone, shines with a second brain

Hermes' built-in memory is small on purpose: `MEMORY.md` is about 2,200 characters and `USER.md` about 1,375.
Dreaming respects that budget. It curates instead of piling up, adds at most 3 facts a night, and merges or
supersedes old entries instead of appending.

If you keep a markdown second brain (an Obsidian vault, an LLM wiki, any folder of notes), point dreaming at it.
Promoted facts then become inbox notes with no size limit, and `MEMORY.md` keeps just one pointer to them.
That's the setup this plugin was built around.

| `destinations` | Where Deep writes | Good for |
|---|---|---|
| `builtin` (default) | `MEMORY.md` / `USER.md`, under their char budget | Everyone |
| `vault` | `<vault>/dreams/YYYY-MM-DD.md` (inbox, one checkbox per fact) + a pointer in `MEMORY.md` | People with a second brain |
| `both` | Both of the above | Second brain + a self-curating prompt memory |

Vault mode only writes inside its own folder, never edits a note it didn't create, and refuses to run if its
folder overlaps one of your `protected_paths`.

## Install

```bash
hermes plugins install victorhurchella/hermes-dreaming --enable
hermes dreaming setup          # copies the cron script, creates the nightly job (03:00)
hermes dreaming run --dry-run -v   # preview what it would do, writes nothing
```

The cron job runs as a script-only job (`--no-agent`), so there is no agent turn. Its only cost is the
dreaming LLM calls, by default at most about 13 per night (12 for extraction, 1 for consolidation per memory
file, 1 for the diary). They go through your own Hermes provider. To run them on a cheaper model:

```yaml
# ~/.hermes/config.yaml
auxiliary:
  dreaming:
    model: <a cheap model on your provider>
```

Optional one-time backfill of the last 14 days (more LLM calls, once):

```bash
hermes dreaming run --max-messages 3000
```

## Configure

All settings are optional. Set them in the Plugins hub of the desktop app, or in `config.yaml`:

```yaml
plugins:
  entries:
    hermes-dreaming:
      settings:
        destinations: vault            # builtin | vault | both
        vault_path: ~/notes            # else $OBSIDIAN_VAULT_PATH, else $WIKI_PATH
        vault_folder: dreams           # the only place dreaming writes in the vault
        protected_paths: [self, journal]
        destructive: stage             # stage merges/removals for /memory pending, or apply
        max_promotions: 3
        min_sessions: 2
        schedule: "0 3 * * *"
        timezone: America/Sao_Paulo
```

`hermes dreaming setup` reads `schedule` once. To change it later, edit the job with `hermes cron edit`.

## Commands

| Command | What it does |
|---|---|
| `hermes dreaming run [--dry-run] [-v] [--no-llm] [--max-messages N]` | Run one sweep now |
| `hermes dreaming status` | Settings, cron job, store size, last runs, pending approvals |
| `hermes dreaming explain "<words>"` | Score breakdown and gates for matching candidates: why something was or wasn't remembered |
| `hermes dreaming diary [-n 3]` | Recent Dream Diary entries |
| `hermes dreaming undo [--force]` | Restore `MEMORY.md`/`USER.md` from the backup taken before the last write |
| `/dreaming [status\|run\|diary\|explain …]` | The same, from any chat |

## Safety

- **Reads only what you said.** Ingestion keeps user turns and the agent's final replies from interactive
  sessions. Tool output, delegation results, cron and subagent sessions, and system notices never enter.
  Group chats are marked untrusted and are never promoted.
- **Recurrence, not one-offs.** A fact needs at least 2 observations in at least 2 distinct sessions,
  durability ≥ 0.5 and a score ≥ 0.65. The one exception is an explicit instruction from you ("remember…",
  "from now on…").
- **Deletions need your approval.** Merges, replacements and evictions go to Hermes' own `/memory pending`
  queue (`/memory approve <id>`). Only plain additions are applied directly. Dreaming may only evict entries
  it created itself.
- **The model plans, the host checks.** A consolidation plan is rejected if it touches unknown entries,
  replaces more than 25% of the store, exceeds the budget, or puts numbers, paths or URLs into a rewritten
  entry that weren't in its sources. A rejected plan falls back to append-only.
- **Every write is reversible.** Writes go through Hermes' `MemoryStore` (lock, atomic write, injection scan).
  A backup is taken before each write, and `hermes dreaming undo` restores it.

## How it maps to OpenClaw

See [docs/how-it-works.md](docs/how-it-works.md): the OpenClaw algorithm with file:line references, what was
kept verbatim, and what changed for Hermes and why. Hermes has no recall log, so "distinct search queries"
becomes "distinct sessions". Hermes has no daily notes, so Light extracts candidates itself.

## Requirements

- Hermes Agent with the plugin API (`register_cli_command`, script-only cron jobs). Developed against v0.23.
- Linux or macOS (uses `fcntl` locks).

## Develop

```bash
cd tests && python3 -m unittest -v     # stdlib only, no Hermes needed
```

## License

MIT. Ports material from OpenClaw (MIT): see [NOTICE](NOTICE). Not affiliated with OpenClaw or Nous Research.
