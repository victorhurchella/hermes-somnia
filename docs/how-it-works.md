# How it works

This is the technical reference: what OpenClaw's Dreaming does, what hermes-dreaming keeps, and
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

| OpenClaw | Hermes reality | hermes-dreaming |
|---|---|---|
| Daily notes `memory/YYYY-MM-DD.md` are written by the agent during the day | Hermes has no daily-notes layer | **Light extracts candidates itself.** One LLM call per chunk of new conversation turns turns raw dialogue into short candidate facts. This is the main cost: by default at most 12 calls per night. |
| `relevance` = memory_search score; `diversity` = distinct search queries | Hermes keeps no recall log | `relevance` = the durability (0–1) the extractor assigned to each observation; `diversity` = **distinct sessions** the fact appeared in. Recurrence across sessions is the strongest signal Hermes can observe. |
| `MEMORY.md` is a large, unbounded file | `MEMORY.md`/`USER.md` have a hard character budget (2,200 / 1,375 by default) | **Curation, not accumulation.** The LLM plan can add, merge into, or supersede existing entries. It may only evict entries dreaming itself created (tracked by hash). It must leave 10% headroom, and at most 3 promotions per night. |
| Inline markers in `MEMORY.md` (`<!-- importance -->`, source refs) | Every character of the budget is prompt space | No inline metadata. Provenance lives in the plugin's SQLite sidecar. |
| Gates: ≥3 recalls, ≥3 unique queries, score ≥ 0.75 | Different signals | ≥2 signals, ≥2 distinct sessions, durability ≥ 0.5, score ≥ 0.65. An explicit owner instruction ("remember…", "from now on…") counts as recurrence on its own. |
| Consolidation writes directly | Hermes has a write-approval queue (`/memory pending`) | Merges, replacements and evictions are **staged for approval by default** (`destructive: stage`). Plain additions are applied. |
| Multi-workspace fan-out, sentinel cron message caught by a hook | Hermes has script-only cron jobs | One `--no-agent` script job. It costs nothing beyond the dreaming LLM calls, and stdout is the delivery. |

REM themes also need a tag to appear in at least 2 candidates (with OpenClaw's formula alone, every tag of a
2-entry pool is a theme). Portuguese stop words were added to the concept-tag filter.

Kept as-is: the Light freshness gate and the Light→REM timestamp handshake, deterministic REM, concept-tag
derivation, the six-signal formula and the phase boost, the provenance gate, managed Markdown blocks, the
diary prompt and its continuity context, and plan validation with a backup before every write. Validation
adds one rule of its own: a rewritten entry may not introduce numbers, paths or URLs that are absent from its
sources.

Dropped: OpenClaw's grounded REM backfill over historical notes (`rem-evidence.ts`, whose regexes are tuned to
one author's notes), multi-workspace fan-out, and QMD/LanceDB integrations.

## 3. The second-brain mode (`destinations: vault`)

With a markdown vault, the size problem goes away: promoted facts become notes, and `MEMORY.md` keeps a single
pointer entry ("Dreaming inbox: …"). The rules that keep the vault clean:

- Dreaming writes **only** inside `vault_folder` (default `dreams/`): one note per night
  (`YYYY-MM-DD.md`, `status: inbox`, each fact as a `- [ ]` checkbox with its evidence) plus `DREAMS.md`.
- It never edits notes it did not create. `protected_paths` lists folders the inbox may not overlap.
- Moving a fact from the inbox into a curated note is a human decision, or an agent decision that the human
  confirms.

`both` writes to the vault and also curates `MEMORY.md`/`USER.md`.

## 4. Files

| Path | What |
|---|---|
| `$HERMES_HOME/plugin-data/hermes-dreaming/state.db` | Short-term store (candidates, signals, phase hits), session cursor, owned-entry hashes, run log |
| `…/reports/YYYY-MM-DD.md` | `## Light Sleep`, `## REM Sleep`, `## Deep Sleep` blocks for the night |
| `…/DREAMS.md` (or `<vault>/<vault_folder>/DREAMS.md`) | Dream Diary and the latest Deep Sleep summary |
| `…/runs/<timestamp>/` | Backups of `MEMORY.md`/`USER.md` taken before a write, plus the applied operations (used by `undo`) |
| `$HERMES_HOME/scripts/hermes-dreaming.py` | Cron shim installed by `hermes dreaming setup` |

## 5. Ingestion firewall

The plugin reads `state.db` read-only and keeps only:
- sessions whose `source` is interactive (desktop, cli, tui and messaging platforms). Cron, subagent and
  one-shot sessions are excluded;
- user turns, minus runtime-injected rows: delegation results, process notices, compaction notices and
  `[IMPORTANT…]` system notes. Mid-turn steering messages are kept, with their wrapper stripped;
- the agent's final text reply of each turn (tool calls and tool output never enter);
- nothing that carries dreaming's own fingerprints, so it never learns from its own output.

Group or channel chats are marked `untrusted` and are never promoted.
