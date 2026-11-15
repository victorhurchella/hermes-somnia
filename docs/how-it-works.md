# How it works

This is the technical reference: what OpenClaw's Dreaming does, what hermes-somnia keeps, and
what it changes for Hermes. File:line references point at OpenClaw `extensions/memory-core/src/`
at commit `9c2e60f7d` (v2026.11.3).

## 1. OpenClaw's Dreaming in one page

One managed cron job (`0 3 * * *` by default) runs one **sweep** per workspace (`dreaming.ts`). A sweep
gets a single timestamp and runs three phases in a fixed order (`dreaming-phases.ts:1151-1214`):

| Phase | What it does | Writes long-term memory? |
|---|---|---|
| Light | Ingests the daily notes `memory/YYYY-MM-DD.md` and session transcripts into a short-term store, dedupes them (Jaccard ≥ 0.9), and stages up to 100 fresh candidates from the last 2 days. | No |
| REM | Rule-based reflection over the staged candidates. Finds recurring concept tags ("themes", strength = min(1, 2·count/N) ≥ 0.75) and up to 3 "possible lasting truths" (confidence ≥ 0.45). | No |
| Deep | Scores every candidate on six weighted signals plus a boost from Light and REM hits, then gates them (minScore 0.75, ≥3 recalls, ≥3 unique queries, ≤30 days old, max 10 per night). The winners go into `MEMORY.md`, consolidated by an LLM plan that the host validates. | **Yes** |

After the phases, a **Dream Diary** entry is written to `DREAMS.md`. It is a short first-person narrative
generated from the staged snippets, themes and promotions, and it is meant for humans to read. The diary
is never used as a source of memory.

The Deep score (`short-term-promotion.ts:25-200`):

```
score = 0.24·frequency + 0.30·relevance + 0.15·diversity + 0.15·recency
      + 0.10·consolidation + 0.06·conceptual + phase_boost

frequency     = log1p(signals) / log1p(10)
relevance     = mean score of the signals (memory_search result scores)
diversity     = distinct interactive search queries / 5
recency       = exp(-ln2/14 · age_days)
consolidation = 0.55·log1p(days-1)/log1p(4) + 0.45·min(1, span_days/7)   (0.2 for a single day)
conceptual    = concept_tags / 6
phase_boost   = 0.06·s(light_hits)·r(last_light) + 0.09·s(rem_hits)·r(last_rem)
                s(h) = log1p(h)/log1p(6), r = 14-day half-life
```

Safety rails in OpenClaw worth knowing:
- Session ingestion keeps only user and assistant text. Tool output is dropped, and so are turns injected by
  memory tools, heartbeats and cron.
- Provenance classes: `owner`, `agent`, `untrusted`, `system`. Only owner and agent material is ever promoted.
- The consolidation model only chooses operations. The host builds the text and validates the plan: the number of
  prior entries a plan may replace is capped at 25%, source references are checked, and there is a budget.
  Every rewrite of `MEMORY.md` is preceded by a backup.

## 2. What changed for Hermes, and why

| OpenClaw | Hermes reality | Somnia |
|---|---|---|
| Daily notes `memory/YYYY-MM-DD.md` are written by the agent during the day | Hermes has no daily-notes layer | **Light extracts candidates itself.** One LLM call per chunk of new conversation turns turns raw dialogue into short candidate facts. This is the main cost: by default at most 12 calls per night. |
| `relevance` = memory_search score; `diversity` = distinct search queries | Hermes keeps no recall log | `relevance` = the durability (0–1) the extractor assigned to each observation; `diversity` = **distinct sessions** the fact appeared in. Recurrence across sessions is the strongest signal Hermes can observe. |
| `MEMORY.md` is a large, unbounded file | `MEMORY.md`/`USER.md` have a hard character budget (2,200 / 1,375 by default) | **Curation, not accumulation.** The LLM plan can add, merge into, or supersede existing entries. It may only evict entries Somnia itself created (tracked by hash). It must leave 10% headroom, and at most 3 promotions per night. |
| Inline markers in `MEMORY.md` (`<!-- importance -->`, source refs) | Every character of the budget is prompt space | No inline metadata. Provenance lives in the plugin's SQLite sidecar. |
| Gates: ≥3 recalls, ≥3 unique queries, score ≥ 0.75 | Different signals | ≥2 signals, ≥2 distinct sessions, durability ≥ 0.5, score ≥ 0.65. An explicit owner instruction ("remember…", "from now on…") counts as recurrence on its own. |
| Consolidation writes directly, with a preimage backup | Hermes has a write-approval queue (`/memory pending`) | Writes directly too (`destructive: apply`, default), with a backup and `hermes somnia undo`. `destructive: stage` sends merges, replacements and evictions to `/memory pending` instead. |
| Multi-workspace fan-out, sentinel cron message caught by a hook | Hermes has script-only cron jobs | One `--no-agent` script job. It costs nothing beyond Somnia's own LLM calls, and stdout is the delivery. |

REM themes also need a tag to appear in at least 2 candidates (with OpenClaw's formula alone, every tag of a
2-entry pool is a theme). Portuguese stop words were added to the concept-tag filter.

Kept as-is: the Light freshness gate and the Light→REM timestamp handshake, deterministic REM, concept-tag
derivation, the six-signal formula and the phase boost, the provenance gate, managed Markdown blocks, the
diary prompt and its continuity context, and plan validation with a backup before every write. Validation
adds one rule of its own: a rewritten entry may not introduce numbers, paths or URLs that are absent from its
sources.

## 2b. What Somnia adds on top of OpenClaw

| Addition | Why |
|---|---|
| Principles (`principles.py`): pareto, lindy, occam, feynman on; inversion opt-in | Concrete curation rules with a benchmark ([bench.md](bench.md)) |
| Verbatim quote per candidate, checked against the transcript | Evidence the user can see; invented quotes cost score |
| Placement: `kind` = fact, preference, rule, correction or procedure | Procedures become skill ideas instead of eating memory |
| Reject list ("forget forever"), re-offered only after 180 days of recurrence | Corrections from the user stick |
| Pin / promote-now | The user can override recurrence |
| Wake gate: a night without new conversation costs zero LLM calls | Cost |
| Extraction budget: the cursor only advances past what was actually read | A busy day is spread over several nights, never skipped |
| Loud no-op alarm, stale-cron alarm, full-budget alarm | The documented OpenClaw failure: a week of 0 promotions while the diary looks healthy |
| Secret masking before the model | Privacy |
| Confirmation tracking for Somnia's own entries, flagged stale after 45 days | Decay without silent deletion |
| Desktop page + `::somnia-memory` chat card + morning report | Review without obligation |

Dropped: OpenClaw's grounded REM backfill over historical notes (`rem-evidence.ts`, whose regexes are tuned to
one author's notes), multi-workspace fan-out, and QMD/LanceDB integrations.

## 2c. Judgment insights (`focus: insights`, the default since 0.4.0)

The fact pipeline above answers "what keeps coming back". On a setup that already has skills, repo docs and a
vault, those facts already have a home, and promoting them reads as filler. What has no home is how the user
judges. `focus` selects what a night looks for: `insights`, `facts` (sections 1 to 2b, unchanged) or `both`.

An insight is a criterion the user applies: a principle they reason with (named or not), a value they weigh above
another, a taste that makes them accept or reject work, or what they want from an answer. The pipeline
(`judgment.py`, `judgment_sinks.py`) is two model calls with the decisions made in code:

| Step | Model | Code |
|---|---|---|
| Extraction, one call per ~40k chars of new turns (own cursor `insight_cursor`, same call budget) | Returns up to 6 observations per chunk: criterion, kind, how it was applied, context, verbatim quote | Keeps an observation only when its quote is found in an owner **user** fragment (agent replies are context, clipped to 220 chars, and never evidence). Session and day come from the fragment that holds the quote. Observations matching a rejected insight are dropped |
| Synthesis, at most one call per night, only when there is something new to group | Groups observations by criterion; for a new group writes title, criterion, area, how the user applies it, and the rule for the agent | A new insight needs ≥2 observations from ≥2 sessions and ≥2 days with ≥2 different quotes. Invalid area/kind, an empty rule or a dangling opening are refused. At most `max_insights` proposals per night, no minimum; the rest wait up to `insight_window_days` |

What happens next is the user's call:

- **Proposed**: a block under `### Insights` in the night's inbox note (one checkbox), the desktop page, and one
  line in the morning report. Nothing else changes.
- **Approved** (ticked checkbox read at the next sweep, the page, or `hermes somnia approve <key>`): the insight
  enters the **living note**, a file Somnia generates from its store, grouped by area. One `USER.md` entry points
  the agent at it, and up to `insights_inline_max` insights of area `responses` are also copied into `USER.md` as
  one-line rules. These entries are additions only; the fact pipeline and Optimize never touch them.
- **Rejected** (`forget`): never proposed again, with no timed re-ask. `unforget` returns it to proposed.

Other rules: an empty night is never a no-op for the alarm; a chunk whose call fails holds the cursor and is
retried, then skipped after 3 failed nights; `vault_pull_command` runs before ticks are read; ticks and the
pointer retry run even on a night with no new conversation; `undo` does not cover insights.
`insights_known_notes` are read so synthesis can mark an insight `new`, `refines` or `known` (not proposed).

## 3. The second-brain mode (`destinations: vault`, or `auto` with a vault configured)

With a markdown vault, the size problem goes away: promoted facts become notes, and `MEMORY.md` keeps a single
pointer entry ("Somnia inbox: …") when it has room (`vault_pointer`; a full `MEMORY.md` only skips the pointer).
The default `destinations: auto` picks this mode whenever `vault_path`, `$OBSIDIAN_VAULT_PATH` or `$WIKI_PATH`
names an existing folder. The rules that keep the vault clean:

- Somnia writes **only** inside `vault_folder` (default `somnia/`): one note per night
  (`YYYY-MM-DD.md`, `status: inbox`, each fact or insight proposal as a `- [ ]` checkbox with its evidence) plus
  `DREAMS.md` and the living note of approved insights (`insights.md`).
- The one exception: `insights_note` may name another file inside the vault, even under a protected folder,
  because the user chose it. Somnia creates that file with a `somnia: insights` frontmatter marker and refuses to
  write to an existing file that lacks it.
- It never edits notes it did not create. `protected_paths` lists folders the inbox may not overlap.
- Moving a fact from the inbox into a curated note is a human decision, or an agent decision that the human
  confirms.
- The notes follow your vault's conventions: `vault_note_type` and `vault_note_tags` set the frontmatter, which
  also carries `created`, `updated` and `source`; `vault_language` sets the headings (en, pt-BR).
- `vault_sync_command` runs once per night after the inbox note and the diary are written, from the vault root
  (`{note}`, `{folder}`, `{day}` are substituted), for vaults synced with git. A failure counts as a night error,
  so a sync that keeps failing raises the errors alarm.

`both` writes to the vault and also curates `MEMORY.md`/`USER.md`.

## 4. Files

| Path | What |
|---|---|
| `$HERMES_HOME/plugin-data/hermes-somnia/state.db` | Short-term store (candidates, evidence, phase hits), session cursor, owned-entry hashes, reject list, per-night change log, run log |
| same `state.db` | Insights: `observations` (quoted occasions) and `judgments` (proposed, approved, rejected), `insight_cursor` |
| `<vault>/<vault_folder>/insights.md` (or `insights_note`; `…/insights.md` without a vault) | The living note of approved insights, regenerated on every change |
| `…/jobs/` | Background jobs started from the desktop page (run, preview, promote) |
| `…/reports/YYYY-MM-DD.md` | `## Light Sleep`, `## REM Sleep`, `## Deep Sleep` blocks for the night |
| `…/DREAMS.md` (or `<vault>/<vault_folder>/DREAMS.md`) | Dream Diary and the latest Deep Sleep summary |
| `…/runs/<timestamp>/` | Backups of `MEMORY.md`/`USER.md` taken before a write, plus the applied operations (used by `undo`) |
| `$HERMES_HOME/scripts/hermes-somnia.py` | Cron shim installed by `hermes somnia setup` |

## 5. Ingestion firewall

The plugin reads `state.db` read-only and keeps only:
- sessions whose `source` is interactive (desktop, cli, tui and messaging platforms). Cron, subagent and
  one-shot sessions are excluded. A delegated subagent can inherit its parent's interactive source, so a session
  with a `parent_session_id` that is a delegated run (`created_source = subagent`, a `Subagent:` title, or an
  `agent_close`/`startup_orphan_reap` end) is skipped too: its "user" turns are briefs the agent wrote;
- user turns, minus runtime-injected rows: delegation results, process notices, compaction notices and
  `[IMPORTANT…]` system notes. Mid-turn steering messages are kept, with their wrapper stripped;
- the agent's final text reply of each turn (tool calls and tool output never enter);
- nothing that carries Somnia's own fingerprints, so it never learns from its own output.

Group or channel chats are marked `untrusted` and are never promoted.
