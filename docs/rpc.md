# Somnia RPC contract (desktop page ⇄ Python half)

Every method is served by the plugin's Python half through `ctx.register_rpc(name, handler)`, so the
wire name is `plugin.hermes-somnia.<name>`. JS calls it with
`host.request('plugin.hermes-somnia.<name>', params, timeoutMs)`. Handlers return plain JSON
(no NaN/Infinity). Floats are rounded to 3 decimals. Times are unix seconds (float). Days are
`YYYY-MM-DD` strings. Unknown params are ignored. Errors raise and reach JS as
`JsonRpcGatewayError` (`code 4001`, `data.reason = 'plugin_rpc_failed'`, `data.detail = '<Exc>: <msg>'`);
`-32601` means the Python half is not enabled.

Never name a param `session_id` (the gateway would authorize the call against that session):
session references use `source_session`.

## Shared shapes

```ts
type Components = {            // each 0..1 except phase_boost (0..0.15) and the principle bonuses
  frequency: number; relevance: number; diversity: number; recency: number;
  consolidation: number; conceptual: number; phase_boost: number;
  lindy?: number;              // 0..0.05, principle bonus (present only when it applies)
  inversion?: number;          // 0.06 for corrections/pitfalls when the principle is on
}
type Weights = { frequency: 0.24; relevance: 0.30; diversity: 0.15; recency: 0.15; consolidation: 0.10; conceptual: 0.06 }
type Gate = 'origin' | 'placement' | 'signals' | 'sessions' | 'durability' | 'age' | 'score' | 'not_promoted' | 'not_rejected'
// placement = false for kind 'procedure' (belongs in a skill, never in memory)
type SessionRef = { id: string; title: string | null; source: string | null; day: string }
type Evidence = { source_session: string; day: string; quote: string }   // quote is verbatim from the user/agent turn, ≤160 chars

type Candidate = {
  key: string;                 // 'claim:<sha1-12>'
  text: string;
  target: 'memory' | 'user';
  kind: 'fact' | 'preference' | 'rule' | 'procedure' | 'correction';
  origin: 'owner' | 'agent' | 'untrusted' | 'system';
  explicit: boolean;           // the owner asked to remember it / set a standing rule
  pinned: boolean;             // pinned from the page: treated like explicit
  score: number;               // 0..1 (clamped raw)
  raw: number;                 // unclamped
  min_score: number;           // the gate in force
  components: Components;
  passes: Record<Gate, boolean>;
  missing: string[];           // human sentences: "needs 1 more session", "score 0.58 < 0.65"
  signal_count: number;
  sessions: SessionRef[];      // newest first, ≤8
  days: string[];
  tags: string[];              // concept tags
  evidence: Evidence[];        // ≤3, newest first
  first_seen_at: number; last_seen_at: number;
  status: 'candidate' | 'promoted' | 'staged' | 'rejected' | 'skill';
  promoted_to: string | null;  // 'builtin:memory' | 'builtin:user' | 'vault:<path>' | 'staged:<id>' | ...
  promoted_at: number | null;
  deferred_reason: string | null;   // e.g. 'memory budget', 'occam: the merge does not compress its parts'
  reask: boolean;              // a forgotten fact that kept recurring and is being offered again
}

type MemoryEntry = {
  index: number;               // position in the file
  text: string;
  chars: number;
  owner: 'somnia' | 'human';   // somnia = created by a dream (only these are ever evicted)
  key: string | null;          // the candidate that produced it (somnia entries)
  created_at: number | null;
  last_confirmed_at: number | null;  // last time a sweep observed the same fact again
  stale: boolean;              // somnia-owned and unconfirmed for > stale_days
  pointer: boolean;            // the "Somnia inbox:" vault pointer entry
}
type Budget = { target: 'memory' | 'user'; used: number; limit: number; goal: number; entries: MemoryEntry[] }

type Night = {                 // one per sweep (latest per day), oldest → newest
  date: string; started_at: number; dry_run: boolean;
  fragments: number; observations: number; staged: number;   // Light
  themes: number; truths: number;                            // REM
  promoted: number; merged: number; deferred: number;        // Deep
  llm_calls: number; quiet: boolean;                         // quiet = no new conversation, zero LLM calls
  noop: boolean;                                             // had material but promoted nothing
  errors: number;
}
type Change = {                // one per Deep operation of a night
  run_at: number;
  key: string;
  action: 'added' | 'merged' | 'superseded' | 'skipped' | 'staged' | 'vault' | 'deferred' | 'evicted';
  // evicted: a Somnia-owned entry removed to make room (key null, text = the entry, reason 'pareto tail' | ...)
  target: 'memory' | 'user' | 'vault';
  text: string;                // the entry as written (or the candidate text)
  replaced: string[];          // prior entries merged/superseded away
  score: number; components: Components;
  sessions: SessionRef[];
  reason: string | null;       // for deferred/skipped
  undoable: boolean;           // true for the latest applied builtin write
}
```

## Read methods

| Method | Params | Returns |
|---|---|---|
| `overview` | `{}` | `Overview` (below). One call paints the page header, gauges, hypnogram and alarm. |
| `changes` | `{ run?: 'latest' \| number }` | `{ run_at: number \| null, date: string \| null, changes: Change[] }` |
| `candidates` | `{ filter?: 'near' \| 'all' \| 'promoted' \| 'rejected' \| 'skills', q?: string, limit?: number }` | `{ items: Candidate[], total: number }`. `near` = not promoted, not rejected, sorted by raw desc. |
| `candidate` | `{ key: string }` | `Candidate` |
| `themes` | `{}` | `{ themes: {tag: string, count: number, strength: number, keys: string[]}[], links: {a: string, b: string, weight: number}[] }` (tags co-occurring in the same candidates) |
| `diary` | `{ limit?: number }` | `{ entries: {date: string, at: number \| null, text: string, fallback: boolean}[], path: string }` newest first; `date` is the display string written in DREAMS.md (e.g. 'November 11, 2026 at 3:00 AM UTC') |
| `stats` | `{}` | `{ histogram: {bucket: number, count: number}[] (20 buckets of 0.05), min_score: number, by_gate: Record<Gate, number> (how many candidates each gate blocks), promoted_total, rejected_total, candidates_total }` |
| `sessions.fed` | `{}` | `{ sessions: Record<string, string[]> }` session id → keys of promoted candidates it fed (for ✦ badges) |
| `job` | `{ id: string }` | `{ id, kind: 'run' \| 'dry_run' \| 'promote', state: 'running' \| 'done' \| 'failed' \| 'cancelled', started_at, finished_at, summary: string, result: object \| null, error: string \| null }` |
| `job.cancel` | `{ id: string }` | `{ ok, state }`. Stops a running **preview** (dry run) or **optimize** proposal and marks it `cancelled`; a late result is discarded. A real night cannot be cancelled (it may be mid-write): `RpcError`. |
| `optimize` | `{ target: 'memory' \| 'user' }` | `{ ok, job }`. Starts a proposal (2 model calls, writes nothing). The finished job's `result` is `{ target, limit, before_chars, after_chars, saved, items: [{ id, from: number[], before: string[], after, saved, ok, reason }], unchanged, llm_calls, error }`. `ok: false` items failed a check (`reason`) and cannot be applied. |
| `optimize.apply` | `{ job: string, accept: number[] }` | `{ ok, applied, saved }`. One MemoryStore batch (`replace` + `remove` for merges), backup + undo manifest. `RpcError` if the originals changed since the proposal. |
| `memory.limit.set` | `{ target, limit: number }` | `{ ok, target, limit, tokens }`. Sets `memory.memory_char_limit` / `memory.user_char_limit` via `hermes config set` (500–20000, never below what is in use). New chats pick it up. |

```ts
type Overview = {
  version: string;
  destinations: 'builtin' | 'vault' | 'both';
  destructive: 'apply' | 'stage';
  schedule: string; timezone: string;
  cron: { installed: boolean; id: string | null; next_run_at: string | null; state: string | null; created_at?: string };
  llm: { provider: string | null; model: string | null; task: string };   // what the sweep will call
  last_run: { at: number; dry_run: boolean; summary: string } | null;
  budgets: Budget[];                      // memory + user (only targets that are enabled)
  nights: Night[];                        // last 30
  alarm: null | {                         // loud no-op detection
    kind: 'noop' | 'stale_cron' | 'errors' | 'budget_full';
    nights: number;                       // consecutive nights affected
    message: string;                      // one sentence, plain language
    best: Candidate | null;               // the closest miss, when kind = noop
    action: 'run' | 'settings' | 'review' | null;
  };
  counts: { candidates: number; near: number; promoted: number; rejected: number; skills: number; stale: number; pending: number };
  vault: null | { path: string; folder: string; inbox_notes: number; open_items: number };
  pressure: { memory: number; user: number };   // used / limit
  principles: {
    active: string[];                     // subset of all
    all: ['pareto', 'lindy', 'inversion', 'occam', 'feynman'];
    labels: Record<string, string>;       // short title, e.g. 'Pareto: keep the vital few'
    descriptions: Record<string, string>; // one plain sentence each
  };
  settings: { destructive: 'apply' | 'stage'; max_promotions: number; min_score: number; min_sessions: number;
              diary: boolean; diary_language: string; notify: 'changes' | 'always' | 'never'; principles: string[] };
}
```

## Write methods

All return `{ ok: true, ... }` or raise. Each one is idempotent where noted.

| Method | Params | Effect |
|---|---|---|
| `forget` | `{ key: string, reason?: string, remove_entry?: boolean }` | Rejects the candidate for good (normalized-text hash). Future sweeps drop matching observations; re-asked only if it keeps recurring after 180 days. With `remove_entry` (default true when promoted to builtin), also removes the memory entry it produced. Returns `{ ok, removed_entry: boolean }`. |
| `unforget` | `{ key }` | Removes it from the reject list. |
| `pin` | `{ key, pinned: boolean }` | Pinned candidates count as an explicit owner instruction at the next sweep (bypass the recurrence gates, not the trust gate). |
| `promote` | `{ key, target?: 'memory' \| 'user' \| 'vault' }` | Starts a background job that writes this one candidate now (same consolidation + validation as the nightly sweep). Returns `{ ok, job: string }`. |
| `entry.edit` | `{ target: 'memory' \| 'user', index: number, old: string, text: string }` | Replaces one memory entry (refuses if `old` no longer matches). Edited somnia entries stay somnia-owned. |
| `entry.remove` | `{ target, index, old, reject?: boolean }` | Removes one entry; `reject` also rejects the candidate behind it. |
| `entry.to_vault` | `{ target, index, old }` | Appends the entry to tonight's vault inbox note and removes it from memory (needs a vault). |
| `undo` | `{}` | Restores MEMORY.md/USER.md from the backup taken before the latest applied write (refuses if they changed since). Returns `{ ok, restored: string[] }`. |
| `run` | `{ dry_run?: boolean }` | Starts a sweep as a background job; returns `{ ok, job }`. Only one at a time (`job` of the running one is returned with `already_running: true`). |
| `skill.dismiss` | `{ key }` | Hides a skill suggestion (rejects it). |
| `settings.set` | `{ key: string, value: any }` | Only for the few toggles the page exposes inline (`destructive`, `max_promotions`, `min_score`, `min_sessions`, `diary`, `diary_language` (`auto` or a language name/code), `notify`, `principles` — a string[] of enabled principles). Everything else lives in the Plugins hub. Returns `{ ok, key, value }`. |

## Inline chat card

The skill teaches the agent to answer "what do you remember about X / why do you know X" by emitting a
paragraph that is only `::somnia-memory{key="claim:..."}`. The desktop half claims the directive and renders
the candidate card (score ring, the six bars, sessions, Forget/Pin buttons) using `candidate`.

## Used by desktop/plugin.js

RPC methods (all via `host.request('plugin.hermes-somnia.<name>', …)`):

- Read: `overview` (polled every 60 s; shared by the page, statusbar chip and empty-chat line), `changes`
  (`{run:'latest'}` and `{run:<started_at>}` from the sleep chart), `candidates` (`filter` near|promoted|rejected|skills,
  `q`, `limit: 100`), `candidate` (`{key}`, inline chat card), `themes`, `diary` (`{limit: 12}`), `stats`, `sessions.fed`,
  `job` (polled every 2 s while a job runs).
- Write: `run` (`{}` and `{dry_run:true}`; dry-run `job.result` is read as `{summary, changes: Change[]}`), `promote`
  (`{key, target?}`), `forget` (`{key, remove_entry}`), `unforget`, `pin` (`{key, pinned}`), `skill.dismiss`,
  `entry.edit` / `entry.remove` (`reject` for "Forget") / `entry.to_vault` (`{target, index, old}`), `undo`,
  `settings.set` for `principles`, `destructive`, `max_promotions`, `min_score`, `min_sessions`, `diary_language`.
- Dry-run jobs: `job.result` is the sweep summary (`fragments`, `promoted`, `merged`, `deferred`, `staged`, `quiet`, …)
  plus `changes: Change[]`, the operations a real night would log (with `undoable: false`).
- UI language: the page picks its own (`auto` follows the app when the app's language is shipped, else the system
  language, else English) and stores the choice in `ctx.storage`. The Python half always answers in English; the
  page translates known reasons and alarms and leaves anything else (errors, model text) as sent.
- `-32601` on `overview` renders the "run `hermes plugins enable hermes-somnia`" empty state.

SDK exports (namespace import, every optional one feature-detected): `host` (`request`, `state.gateway`, `navigate`,
`openSession(id,{intent:'stack'})`, `newChat`, `composer.setDraft('new', …)` + `composer.focus`, `notify`, `notifyError`),
`useQuery`, `useMutation`, `useQueryClient` (fallback `queryClient`), `useValue`, `usePluginI18n`, `relativeTime`,
`pluginSettingsHref`, `Button`, `Codicon`, `ConfirmDialog`, `Dialog`, `DialogContent`, `DialogHeader`, `DialogTitle`,
`DialogDescription`, `DialogFooter`, `SearchField`, `SegmentedControl`, `Skeleton`, `Streamdown`, `Switch`, `Textarea`,
`Tip`, `ROUTES_AREA`, `SIDEBAR_NAV_AREA`, `PALETTE_AREA`, `KEYBINDS_AREA` (`mod+alt+s`), `STATUSBAR_AREAS.right`,
`TRANSCRIPT_DIRECTIVE_AREA` (`somnia-memory`), `SESSION_ROW_AREAS.trailing`, `CHAT_EMPTY_AREA`. ctx: `registerMany`,
`i18n.register/t/onLocaleChange`, `storage.get/set` (seen run, dismissed cards), `onDispose`.

Gaps the page works around (server could add): a stable change id (page keys "Keep" on `run_at:key|text`, kept only
client-side); `job.result` shape for dry runs is not specified; a `recent`/`since` window for `sessions.fed`; an
`entry_index` on `Change` (the page matches the entry by key or exact text); `diary.entries[].date` is shown verbatim.
