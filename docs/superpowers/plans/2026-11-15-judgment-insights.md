# Judgment Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Somnia 0.4.0 looks for the user's criteria of judgment (insights), proposes them on evidence from two or more occasions, and makes approved ones used through a living note, a `USER.md` pointer and up to three inline entries.

**Architecture:** A second pipeline next to Light → REM → Deep, selected by `focus`. `judgment.py` holds extraction, synthesis and their code-side gates; `judgment_sinks.py` holds everything that writes (inbox section, ticks, living note, `USER.md`). `sweep.py` routes by `focus`. The store gains two tables and one column. The fact pipeline is untouched except for the ingestion filter.

**Tech Stack:** Python standard library, SQLite, `unittest` with the scripted model in `tests/helpers.py`; the desktop page is plain JS in `desktop/plugin.js`.

**Spec:** `docs/superpowers/specs/2026-11-15-judgment-insights-design.md`

## Global Constraints

- Public MIT repo: no personal data in code, tests, fixtures, screenshots or docs. All examples fictional.
- Python standard library only; model access only through the `complete(system, user, max_tokens) -> str` callable.
- Quotes count as evidence only when found verbatim in an owner **user** fragment.
- A new insight needs ≥2 observations from ≥2 sessions and ≥2 days with ≥2 distinct normalised quotes.
- Nothing reaches the living note or `USER.md` without approval; a rejected insight is never proposed again.
- Somnia writes only: its state dir, the inbox folder, the one `insights_note` file, its own owned entries.
- Defaults: `focus=insights`, `max_insights=3`, `insight_window_days=60`, `insights_pointer=true`,
  `insights_inline_max=3`, `insights_note=""` (resolves to `<vault_folder>/insights.md`),
  `insights_known_notes=[]`, `vault_pull_command=""`. Insight chunk size 40,000 chars; agent context clip 220;
  quote ≤160; inline rule ≤160 chars.
- Commits: repo convention (`feat:` / `fix:` English subject), repo git identity, no AI trailer, no push.
- Tests: `cd tests && python3 -m unittest` must stay green after every task (58 tests at the start).

## Review Focus

1. A subagent session whose `source` is interactive: its briefs must never become user quotes (Task 1).
2. The same sentence quoted twice, or two quotes from one day: must not make an insight (Task 3).
3. The user ticks a proposal and edits its text in the note: the stored text is approved (Task 4).
4. `insights_note` pointing at a hand-written file: refused, the insight stays `proposed` (Task 4).
5. `USER.md` full: approval still succeeds, pointer and inline are skipped with a warning, nothing evicted (Task 4).

---

### Task 1: Ingestion filter for delegated sessions

**Files:** Modify `hermes_somnia/ingest.py` (`read_fragments`), `tests/helpers.py` (`make_sessions_db`), Test `tests/test_judgment.py` (new).

**Interfaces:** Produces `make_sessions_db(path, rows, sessions=None)` where `sessions` maps session id → dict of extra `sessions` columns (`parent_session_id`, `created_source`, `title`, `end_reason`); the table always has those columns unless `legacy=True`.

- [ ] Test `test_delegated_sessions_are_skipped`: sessions `s1` (plain), `sub` (`parent_session_id='s1'`, `title='Subagent: build it'`, source `desktop`), `cont` (`parent_session_id='s1'`, no title, `end_reason=None`); `read_fragments` returns fragments of `s1` and `cont` only. `test_legacy_schema_without_columns` (`legacy=True`) still returns `s1`.
- [ ] Implement: build a `not (...)` SQL clause only from the columns present in `sessions` (`pragma table_info`): delegated = `parent_session_id is not null and (created_source='subagent' or title like 'Subagent:%' or end_reason in ('agent_close','startup_orphan_reap'))`; with no `parent_session_id` column the clause is omitted.
- [ ] Run the suite; commit `fix: skip delegated subagent sessions at ingestion`.

### Task 2: Settings and store

**Files:** Modify `hermes_somnia/config.py`, `hermes_somnia/store.py`, `plugin.yaml` (`config_schema`). Test `tests/test_judgment.py`.

**Interfaces (produces):**
- `Settings` fields from Global Constraints, plus properties `wants_facts` (`focus in ('facts','both')`) and `wants_insights` (`focus in ('insights','both')`); an unknown `focus` value falls back to `insights`.
- `store.Judgment` dataclass: `key, title, kind, area, name, criterion, user_version, rule, relation='new', delta=None, status='proposed', proposed_at=0.0, decided_at=None, note=None, inline_hash=None`.
- `Store.add_observation(*, criterion, kind, name, application, context, quote, session_id, day, at, created_at) -> int`
- `Store.observations(*, unassigned: bool = False, key: str | None = None) -> list[dict]` (ordered by `at`)
- `Store.assign_observations(ids: list[int], key: str)`, `Store.drop_observations(ids: list[int])`, `Store.prune_observations(before: float) -> int` (unassigned only)
- `Store.judgments(status: str | None = None) -> list[Judgment]`, `Store.judgment(key) -> Judgment | None`, `Store.save_judgment(j)`
- `owned` gains column `class text` (null = fact entry, `'pointer'`, `'inline'`); `Store.own(..., klass: str | None = None)`; `Store.owned()` rows carry `class`.

- [ ] Tests: round-trip of an observation and a judgment; `prune_observations` leaves assigned rows; opening a 0.3.2 store (create the old schema by hand) adds tables and the column; `Settings.from_mapping({'focus': 'x'}).wants_insights` is true.
- [ ] Implement; suite; commit `feat: insights settings and store tables`.

### Task 3: Extraction and synthesis (`hermes_somnia/judgment.py`)

**Files:** Create `hermes_somnia/judgment.py`. Modify `hermes_somnia/phases.py` (`ground_quote` gains `roles: tuple = ('user', 'assistant')`), `tests/helpers.py` (`ScriptedLLM` gains `judge_extract`, `judge_synth` callables, routed by the system prompts' first words `You read fragments of conversations between a user (the owner) and their AI agent. Agent replies are truncated` and `You receive judgment observations`). Test `tests/test_judgment.py`.

**Interfaces (produces):**
- `KINDS = ('principle','value','taste','response')`, `AREAS = ('design','product','analysis','code','communication','responses')`, `CHUNK_CHARS = 40_000`, `AGENT_CLIP = 220`, `QUOTE_CHARS = 160`
- `judgment_key(criterion: str) -> str` → `'insight:' + sha1_12(normalize_claim(criterion))`
- `@dataclass JudgmentResult: fragments=0, llm_calls=0, llm_errors=0, observations=0, ungrounded=0, rejected_hits=0, proposed: list = [], attached=0, approved: list = [], warnings: list = [], errors: list = [], cursor=(0, 0), quiet=False`
- `extract(store, fragments, complete, settings, now, result) -> int` (new cursor; fragments already masked; reads/writes kv `insight_chunk_fail` = `{first_message_id, nights}`)
- `synthesize(store, complete, settings, known_text: str, now, result) -> list[Judgment]` (new proposals saved with `status='proposed'`; kv `insight_synth_retry` = bool)
- `occasions(store, key) -> list[dict]` (`day, context, quote, source_session`), newest first.

Rules to pin in tests (each its own test, fictional data such as "ship the smallest change that works"):
- [ ] `extract` keeps an observation only when its quote grounds in a user fragment; a quote found only in an agent fragment is dropped and counted in `ungrounded`; session and day come from the grounding fragment.
- [ ] `extract` drops observations whose criterion matches a rejected judgment (same key, or `jaccard ≥ 0.85` with its criterion) and counts `rejected_hits`; drops `untrusted` chunks.
- [ ] `extract` returns the id of the last message of the last chunk that parsed; a chunk whose call raises or is unparsable stops the loop, the cursor stays before it, `llm_errors` is 1; on the third night failing on the same first message id the chunk is skipped with an error line.
- [ ] `extract` with `complete=None` does nothing and returns the old cursor.
- [ ] `synthesize` creates a proposal for 2 observations on 2 days in 2 sessions with 2 different quotes; refuses (observations stay unassigned) for: same day, same session, same normalised quote, 1 observation, invalid `area`/`kind`, empty `rule`, `criterion` starting with "This".
- [ ] `synthesize`: a group with `ref` = a proposed/approved key attaches its observations and bumps `attached`; `ref` = a rejected key drops them; `relation: 'known'` drops them and stores nothing; an id used in two groups counts in the first only.
- [ ] `synthesize` keeps at most `max_insights` new proposals by number of occasions and sets `insight_synth_retry`; makes no call when no trigger holds (no new observation, no retry flag); a raising call sets the retry flag and adds an error.
- [ ] Synthesis user payload contains `ALREADY RECORDED`, existing insights of every status and the unassigned observations within `insight_window_days`; older unassigned ones are pruned first.
- [ ] Implement; prompts start from the spec appendix, with `rule` + `area` + `ref` and without `background`; insight text language from `fact_language` (same override sentence as `light_extract`). Commit `feat: judgment extraction and synthesis`.

### Task 4: Writing: inbox section, ticks, living note, USER.md (`hermes_somnia/judgment_sinks.py`)

**Files:** Create `hermes_somnia/judgment_sinks.py`. Modify `hermes_somnia/sinks.py` (`VAULT_TEXT` labels + intro line; `entry_flags`/eviction and `optimize.py` skip owned rows whose `class` is set). Test `tests/test_judgment.py`.

**Interfaces (produces):**
- `proposal_block(settings, j: Judgment, occ: list[dict]) -> list[str]` (spec §3.3 layout)
- `write_proposals(settings, store, proposals, day, *, dry_run) -> tuple[Path | None, list[str]]` (appends `### Insights` to the nightly note; sets `j.note`)
- `read_ticks(settings, store) -> list[str]` (keys ticked in notes that hold a `proposed` judgment; unknown ticked keys → not returned, reported through the `warnings` list argument)
- `living_note_path(settings) -> Path`; `render_living_note(settings, store) -> str`; `write_living_note(settings, store) -> Path` (atomic: temp file + `os.replace`; raises `RuntimeError` on a file without the `somnia: insights` marker or a path outside the vault)
- `approve(store, settings, backend, key, now) -> dict` → `{'ok': True, 'note': str, 'warnings': [...], 'inlined': bool}`; raises `KeyError` for an unknown key, `RuntimeError` when the living note is refused (status stays `proposed`)
- `forget(store, settings, backend, key, reason) -> dict`; `unforget(store, settings, key) -> bool`
- `POINTER_TEXT = {'en': 'Judgment criteria of the user: {path}. Read the matching section before design, product, analysis, code or writing work.', 'pt-BR': 'Critérios de julgamento do usuário: {path}. Ler a seção correspondente antes de trabalho de design, produto, análise, código ou escrita.'}`

Tests:
- [ ] Living note: frontmatter has `somnia: insights` and `type: <vault_note_type>`, no `status:`; one `## <area>` per area in `AREAS` order with only approved insights; regenerating is byte-identical.
- [ ] `write_living_note` refuses an existing file without the marker; refuses `insights_note='../x.md'`; accepts a path inside a `protected_paths` folder when the file is absent or carries the marker.
- [ ] Tick: note block with `- [x]` and an edited title approves the stored text; `- [ ]` does not; a ticked unknown key yields a warning.
- [ ] `approve` adds the pointer to `USER.md` once (owned, class `pointer`); with `USER.md` at its limit it returns a warning and writes nothing there; with `targets=['memory']` it warns and skips.
- [ ] Inline: an approved `responses` insight with a rule ≤160 chars is added (class `inline`, `inline_hash` set); the fourth is not inlined; a 161-char rule is not; `forget` on an inlined one removes the entry and frees the slot.
- [ ] `forget` on approved removes it from the living note and the inbox block (reuse `remove_from_vault`), sets `rejected`; `unforget` returns it to `proposed`.
- [ ] Fact consolidation flags never list a `pointer`/`inline` owned entry as evictable.
- [ ] Builtin mode (no vault): living note at `<state dir>/insights.md`.
- [ ] Implement; commit `feat: insight approval, living note and USER.md entries`.

### Task 5: Sweep routing, CLI, RPC

**Files:** Modify `hermes_somnia/sweep.py`, `hermes_somnia/cli.py`, `hermes_somnia/rpc.py`, `hermes_somnia/insights.py`, `docs/rpc.md`. Test `tests/test_judgment.py`.

**Interfaces (produces):**
- `SweepSummary.judgment: JudgmentResult`; `as_dict()` gains `focus`, `insights: {observations, proposed: [keys], attached, approved: [keys], waiting}`; `noop` is false when `focus == 'insights'`; `changed` is true when an insight was proposed or approved.
- Sweep order under insights: `vault_pull_command` → ticks → pointer retry → extraction (own cursor kv `insight_cursor`; first run starts `ingest_days` back) → synthesis → proposals into the inbox note → living note if occasions changed → diary (only when proposed/approved) → sync. Ticks and pointer retry run even on a quiet night. `dry_run` writes nothing and lists proposals in `summary.vault_lines`.
- `run_vault_pull(settings) -> str | None` in `sinks.py`.
- CLI: `hermes somnia approve <key>`; `forget`/`unforget`/`explain` accept `insight:` keys; `status` prints `insights: N to review, N approved, N waiting observation(s)`; `promote`/`pin` on an `insight:` key print that they are fact commands.
- RPC: `insights` → `{focus, to_review: Insight[], approved: Insight[], waiting: number, note: string | null}`; `insight.approve {key}`; `forget`/`unforget` accept insight keys; `candidate {key}` returns an `Insight` for insight keys; `overview.counts` gains `insights_to_review`, `insights_approved`; `overview.focus`. `Insight` = the §3.2 fields plus `key`, `status`, `occasions: {day, context, quote, source_session, title}[]`, `days: number`, `first_day`, `last_day`, `inlined: boolean`. Page/CLI writes take `sweep_lock` and raise `RpcError('Somnia is running a sweep; try again in a moment')` when busy.
- Morning report line: `✦ Somnia has N insight(s) for you to review` + titles.

Tests:
- [ ] End-to-end with the scripted model: two sessions on two days → night 1 proposes one insight, inbox note has the block, `USER.md` untouched, summary `noop` false; tick the box → night 2 (no new turns) approves it, living note and pointer exist.
- [ ] `focus='facts'` behaves as 0.3.2 (existing suite) and makes no judgment call; `focus='both'` advances both cursors independently.
- [ ] No-op alarm does not fire after 3 empty insight nights.
- [ ] `dry_run` leaves store, vault and memory unchanged.
- [ ] Implement; update `docs/rpc.md`; commit `feat: insights pipeline in the sweep, CLI and RPC`.

### Task 6: Desktop page

**Files:** Modify `desktop/plugin.js`, `dashboard/plugin_api.py` if it lists methods, `dev/harness/*` (mock host + fixtures with fictional insights), screenshots in `docs/screenshots/{en,pt-BR}/`.

- [ ] Insights section per spec §3.6 on the RPC contract of Task 5; strings in English and pt-BR; fact sections hidden when `overview.focus == 'insights'`; `::somnia-memory{key="insight:…"}` card.
- [ ] `node --check desktop/plugin.js`; regenerate screenshots with `dev/harness/screenshots.sh`; look at one key shot per language.
- [ ] Commit `feat(desktop): Insights section`.

### Task 7: Docs and release

**Files:** `README.md`, `README.pt-BR.md`, `docs/how-it-works.md`, `skills/somnia/SKILL.md`, `plugin.yaml`, `catalog/hermes-somnia.yaml`, `hermes_somnia/__init__.py`, `dashboard/manifest.json` if it tracks the plugin version.

- [ ] Version 0.4.0 everywhere it is declared; README "what it does" leads with insights, names `focus: facts` as the previous behaviour, updates the disclosure (new file written, known notes read, pull command); how-it-works gains the pipeline and the "writes only inside `vault_folder`" exception.
- [ ] Full suite, `python bench/run.py --seeds 5`, `node --check desktop/plugin.js`; personal-data grep over `git log -p origin/main..HEAD`.
- [ ] Commit `docs: Somnia 0.4.0 — judgment insights`.
