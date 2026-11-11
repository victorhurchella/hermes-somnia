#!/usr/bin/env node
// Builds dev/harness/fixtures/{demo,empty}.json — a FICTIONAL dataset that
// follows docs/rpc.md field for field. Deterministic: same output every run.
//
//   node dev/harness/fixtures/make-fixtures.mjs
//
// Persona: Maya Okafor, an indie developer building "Lanternfall" (a cozy
// Godot roguelike) and "Ledgerly" (invoicing SaaS for freelancers).
// The harness pins the clock to `now` so relative times are stable.

import { writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const NOW = Date.UTC(2026, 10, 11, 9, 12, 0) / 1000 // Wed 2026-11-11 09:12 UTC
const DAY = 86400
const W = { frequency: 0.24, relevance: 0.3, diversity: 0.15, recency: 0.15, consolidation: 0.1, conceptual: 0.06 }
const MIN_SCORE = 0.65

let seed = 7
const rnd = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646
const r3 = x => Math.round(x * 1000) / 1000
const dayOf = t => new Date(t * 1000).toISOString().slice(0, 10)
const sha = s => {
  let h = 2166136261
  for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0
  return (h.toString(16) + (h * 7).toString(16)).padEnd(12, '0').slice(0, 12)
}

// ---------------------------------------------------------------- sessions
const SESSIONS = [
  ['s-lf-shader', 'Lanternfall: lantern falloff shader', 'desktop', 1],
  ['s-lf-itch', 'Friday build notes for itch.io', 'desktop', 1],
  ['s-lf-tiles', 'TileMapLayer migration (Godot 4.4)', 'desktop', 3],
  ['s-lf-oil', 'Oil meter + lantern radius tuning', 'desktop', 2],
  ['s-lf-save', 'Save system: JSON vs Resource', 'cli', 6],
  ['s-lg-stripe', 'Ledgerly: Stripe webhook retries', 'desktop', 1],
  ['s-lg-drizzle', 'Drizzle migration for invoice_items', 'desktop', 2],
  ['s-lg-neon', 'Neon branch per preview deploy', 'cli', 4],
  ['s-lg-pdf', 'Invoice PDF rendering bug', 'desktop', 5],
  ['s-lg-tax', 'VAT rules for EU freelancers', 'telegram', 8],
  ['s-me-pt', 'Bate-papo: planejamento da semana', 'telegram', 1],
  ['s-me-review', 'Code review tone', 'desktop', 3],
  ['s-me-coffee', 'Morning planning', 'desktop', 9],
  ['s-me-commit', 'Commit message style', 'cli', 12]
].map(([id, title, source, ago]) => ({ id, title, source, day: dayOf(NOW - ago * DAY + 3600) }))
const S = Object.fromEntries(SESSIONS.map(s => [s.id, s]))
const ref = id => ({ id, title: S[id].title, source: S[id].source, day: S[id].day })

// -------------------------------------------------------------- candidates
// [text, target, kind, sessions[], tags[], quotes[], comps{}, extra]
const RAW = [
  // promoted last night -------------------------------------------------
  ['Maya ships Lanternfall builds to itch.io every Friday; release notes stay short and in her own voice.', 'memory', 'fact',
    ['s-lf-itch', 's-lf-oil', 's-lf-tiles'], ['lanternfall', 'itch-io', 'release'],
    ['ok friday build is up on itch, keep the notes short like last time', 'notes should sound like me, not like a press release'],
    { frequency: 0.72, relevance: 0.86, diversity: 0.6, recency: 0.95, consolidation: 0.62, conceptual: 0.5, phase_boost: 0.06 },
    { status: 'promoted', promoted_to: 'builtin:memory', promoted_ago: 0.26 }],
  ['Ledgerly runs on Next.js 15 + Postgres 16 on Neon; schema changes go through Drizzle migrations, never Prisma.', 'memory', 'rule',
    ['s-lg-drizzle', 's-lg-neon', 's-lg-stripe', 's-lg-pdf'], ['ledgerly', 'drizzle', 'postgres', 'neon'],
    ['please never suggest prisma for ledgerly again, we are on drizzle', 'neon branch per preview deploy is working nicely'],
    { frequency: 0.81, relevance: 0.9, diversity: 0.8, recency: 0.88, consolidation: 0.74, conceptual: 0.67, phase_boost: 0.08, lindy: 0.02 },
    { status: 'promoted', promoted_to: 'builtin:memory', promoted_ago: 0.26, explicit: true }],
  ['Maya prefers casual chat in Portuguese, but code, comments and commit messages in English.', 'user', 'preference',
    ['s-me-pt', 's-me-commit', 's-lf-shader'], ['language', 'portuguese', 'commits'],
    ['pode falar comigo em português, mas commit sempre em inglês', 'comments in english please, the repo is public'],
    { frequency: 0.64, relevance: 0.82, diversity: 0.6, recency: 0.93, consolidation: 0.55, conceptual: 0.5, phase_boost: 0.07 },
    { status: 'promoted', promoted_to: 'builtin:user', promoted_ago: 0.26 }],
  // deferred last night (still a candidate) ----------------------------
  ["Lanternfall's lantern radius scales with remaining oil (0–100), easing out below 20.", 'memory', 'fact',
    ['s-lf-oil', 's-lf-shader'], ['lanternfall', 'lantern', 'gameplay'],
    ['radius should ease out hard below 20 oil, it feels like the dark closing in'],
    { frequency: 0.48, relevance: 0.78, diversity: 0.4, recency: 0.97, consolidation: 0.42, conceptual: 0.5, phase_boost: 0.05 },
    { deferred_reason: 'occam: merging it into the oil-meter note would not make either shorter; kept apart for now' }],
  // older promotions ---------------------------------------------------
  ['Godot 4.4: use TileMapLayer — the old TileMap node is deprecated in Lanternfall.', 'memory', 'rule',
    ['s-lf-tiles', 's-lf-save'], ['godot', 'lanternfall', 'tilemap'],
    ['tilemap is deprecated in 4.4, move everything to TileMapLayer'],
    { frequency: 0.5, relevance: 0.84, diversity: 0.4, recency: 0.8, consolidation: 0.6, conceptual: 0.5, phase_boost: 0.05 },
    { status: 'promoted', promoted_to: 'vault:dreams/2026-11-08.md', promoted_ago: 3.26 }],
  ['Stripe webhooks for Ledgerly are idempotent by event id; retries are safe to replay.', 'memory', 'fact',
    ['s-lg-stripe', 's-lg-pdf'], ['ledgerly', 'stripe', 'webhooks'],
    ['we dedupe on event.id so replaying from the dashboard is fine'],
    { frequency: 0.55, relevance: 0.8, diversity: 0.4, recency: 0.78, consolidation: 0.58, conceptual: 0.5, phase_boost: 0.04 },
    { status: 'promoted', promoted_to: 'builtin:memory', promoted_ago: 4.26 }],
  ['Maya reviews code best with blunt, numbered findings and no praise padding.', 'user', 'preference',
    ['s-me-review', 's-lg-pdf', 's-lf-save'], ['review', 'tone'],
    ['just number the problems, skip the compliments'],
    { frequency: 0.58, relevance: 0.83, diversity: 0.6, recency: 0.7, consolidation: 0.66, conceptual: 0.33, phase_boost: 0.03, lindy: 0.04 },
    { status: 'promoted', promoted_to: 'builtin:user', promoted_ago: 7.26 }],
  ['Lanternfall save files are JSON in user://saves, versioned with a "v" field.', 'memory', 'fact',
    ['s-lf-save', 's-lf-tiles'], ['lanternfall', 'save-system', 'godot'],
    ['json saves, put a v field so we can migrate later'],
    { frequency: 0.55, relevance: 0.8, diversity: 0.4, recency: 0.6, consolidation: 0.66, conceptual: 0.5, phase_boost: 0.04 },
    { status: 'promoted', promoted_to: 'builtin:memory', promoted_ago: 12.26 }],
  ['Correction: Ledgerly never hardcodes VAT rates; they are read from the tax_rates table per country.', 'memory', 'correction',
    ['s-lg-tax', 's-lg-pdf', 's-lg-stripe'], ['ledgerly', 'vat', 'invoicing'],
    ['no no, never hardcode 23%, read it from tax_rates', 'that bug again, the rate came from a constant instead of the table'],
    { frequency: 0.5, relevance: 0.86, diversity: 0.6, recency: 0.96, consolidation: 0.4, conceptual: 0.5, phase_boost: 0.06 },
    { status: 'promoted', promoted_to: 'builtin:memory', promoted_ago: 0.26 }],
  ['Maya wants a short Monday digest of new Ledgerly sign-ups.', 'user', 'preference',
    ['s-me-coffee', 's-lg-stripe'], ['ledgerly', 'planning'],
    ['can you give me the signups every monday, just the number and who'],
    { frequency: 0.46, relevance: 0.62, diversity: 0.4, recency: 0.7, consolidation: 0.5, conceptual: 0.33, phase_boost: 0.02 },
    { reask: true }],
  // near misses --------------------------------------------------------
  ['Ledgerly invoices must show VAT reverse-charge wording for EU B2B clients.', 'memory', 'rule',
    ['s-lg-tax'], ['ledgerly', 'vat', 'invoicing'],
    ['reverse charge text has to be on the invoice for b2b in the EU'],
    { frequency: 0.46, relevance: 0.88, diversity: 0.2, recency: 0.6, consolidation: 0.2, conceptual: 0.5, phase_boost: 0.03 },
    { missing: ['needs 1 more session (seen in 1 of 2)', 'came from a group chat — marked untrusted, never promoted'], origin: 'untrusted' }],
  ['Preview deploys of Ledgerly each get their own Neon branch, deleted on merge.', 'memory', 'fact',
    ['s-lg-neon', 's-lg-drizzle'], ['ledgerly', 'neon', 'deploy'],
    ['branch per preview, and the github action deletes it on merge'],
    { frequency: 0.46, relevance: 0.74, diversity: 0.4, recency: 0.74, consolidation: 0.38, conceptual: 0.5, phase_boost: 0.03 },
    {}],
  ['Lanternfall targets 60 fps on a Steam Deck; profile with the Deck before every Friday build.', 'memory', 'rule',
    ['s-lf-shader', 's-lf-itch'], ['lanternfall', 'performance', 'steam-deck'],
    ['if it drops under 60 on the deck it does not ship'],
    { frequency: 0.4, relevance: 0.82, diversity: 0.4, recency: 0.92, consolidation: 0.2, conceptual: 0.5, phase_boost: 0.04 },
    {}],
  ['Maya plans the week on Monday mornings over coffee and likes a 3-item focus list.', 'user', 'preference',
    ['s-me-coffee', 's-me-pt'], ['planning', 'routine'],
    ['me dá só 3 coisas pra focar essa semana', 'monday coffee planning again'],
    { frequency: 0.46, relevance: 0.66, diversity: 0.4, recency: 0.62, consolidation: 0.66, conceptual: 0.33, phase_boost: 0.02 },
    {}],
  ['Invoice PDFs are rendered with react-pdf on the server, never in the browser.', 'memory', 'fact',
    ['s-lg-pdf'], ['ledgerly', 'pdf', 'invoicing'],
    ['pdf has to render server side, the browser version breaks fonts'],
    { frequency: 0.3, relevance: 0.72, diversity: 0.2, recency: 0.7, consolidation: 0.2, conceptual: 0.5, phase_boost: 0.02 },
    {}],
  ['Pixel art in Lanternfall is drawn at 16×16 and scaled 4× with nearest-neighbour filtering.', 'memory', 'fact',
    ['s-lf-tiles', 's-lf-shader'], ['lanternfall', 'pixel-art'],
    ['16x16 tiles, 4x scale, nearest filter or it gets blurry'],
    { frequency: 0.4, relevance: 0.7, diversity: 0.4, recency: 0.77, consolidation: 0.34, conceptual: 0.33, phase_boost: 0.02 },
    {}],
  ['Maya avoids Tailwind in Ledgerly email templates; she hand-writes inline styles.', 'memory', 'preference',
    ['s-lg-pdf', 's-lg-stripe'], ['ledgerly', 'email'],
    ['for emails just inline the styles, tailwind there is pain'],
    { frequency: 0.3, relevance: 0.6, diversity: 0.4, recency: 0.6, consolidation: 0.2, conceptual: 0.33, phase_boost: 0.01 },
    {}],
  ['The Lanternfall soundtrack is by a friend, Tomás; credit him on every itch.io page.', 'memory', 'rule',
    ['s-lf-itch'], ['lanternfall', 'credits', 'itch-io'],
    ['always credit tomás for the music on the itch page'],
    { frequency: 0.3, relevance: 0.9, diversity: 0.2, recency: 0.95, consolidation: 0.2, conceptual: 0.5, phase_boost: 0.03 },
    {}],
  ['Ledgerly free tier allows 5 invoices a month.', 'memory', 'fact',
    ['s-lg-stripe'], ['ledgerly', 'pricing'],
    ['free tier is 5 invoices a month for now'],
    { frequency: 0.2, relevance: 0.52, diversity: 0.2, recency: 0.5, consolidation: 0.2, conceptual: 0.17, phase_boost: 0 },
    {}],
  // forgotten ------------------------------------------------------------
  ['Maya is thinking about moving Ledgerly to Rails.', 'memory', 'fact',
    ['s-lg-drizzle', 's-me-coffee'], ['ledgerly', 'rails'],
    ['lol sometimes i think about rewriting this in rails'],
    { frequency: 0.4, relevance: 0.4, diversity: 0.4, recency: 0.5, consolidation: 0.4, conceptual: 0.33, phase_boost: 0 },
    { status: 'rejected' }],
  ["Maya's cat is called Pixel.", 'user', 'fact',
    ['s-me-coffee'], ['personal'],
    ['pixel is sitting on my keyboard again'],
    { frequency: 0.2, relevance: 0.5, diversity: 0.2, recency: 0.4, consolidation: 0.2, conceptual: 0.17, phase_boost: 0 },
    { status: 'rejected' }],
  ['Use the staging Stripe key in demos.', 'memory', 'rule',
    ['s-lg-stripe'], ['stripe'],
    ['use the staging key for the demo tomorrow'],
    { frequency: 0.2, relevance: 0.55, diversity: 0.2, recency: 0.3, consolidation: 0.2, conceptual: 0.17, phase_boost: 0 },
    { status: 'rejected' }],
  // procedures / skill ideas ------------------------------------------
  ['Friday release: bump version in project.godot, export Linux + Windows, butler push to itch, then post the notes.', 'memory', 'procedure',
    ['s-lf-itch', 's-lf-oil', 's-lf-tiles'], ['lanternfall', 'release', 'itch-io'],
    ['same as every friday: bump, export both, butler push, notes'],
    { frequency: 0.66, relevance: 0.8, diversity: 0.6, recency: 0.95, consolidation: 0.62, conceptual: 0.5, phase_boost: 0.05 },
    { status: 'skill' }],
  ['New Drizzle migration: generate, review SQL by hand, apply to the Neon preview branch, then main.', 'memory', 'procedure',
    ['s-lg-drizzle', 's-lg-neon'], ['ledgerly', 'drizzle', 'neon'],
    ['generate it, I read the sql, preview branch first, then main'],
    { frequency: 0.5, relevance: 0.78, diversity: 0.4, recency: 0.8, consolidation: 0.5, conceptual: 0.5, phase_boost: 0.03 },
    { status: 'skill' }],
  ['Weekly planning: list open threads from both projects, pick 3, block Friday afternoon for the build.', 'user', 'procedure',
    ['s-me-coffee', 's-me-pt'], ['planning', 'routine'],
    ['open threads, pick 3, friday afternoon is build time'],
    { frequency: 0.46, relevance: 0.64, diversity: 0.4, recency: 0.6, consolidation: 0.6, conceptual: 0.33, phase_boost: 0.01 },
    { status: 'skill' }],
  // stale somnia memory -----------------------------------------------
  ['Ledgerly emails go through Postmark.', 'memory', 'fact',
    ['s-lg-pdf', 's-lg-tax'], ['ledgerly', 'email'],
    ['postmark is sending the invoice emails'],
    { frequency: 0.5, relevance: 0.7, diversity: 0.4, recency: 0.12, consolidation: 0.6, conceptual: 0.33, phase_boost: 0 },
    { status: 'promoted', promoted_to: 'builtin:memory', promoted_ago: 41.26 }],
  ['Maya works in Lisbon time (WET/WEST).', 'user', 'fact',
    ['s-me-coffee', 's-me-pt'], ['timezone'],
    ['its 9am here in lisbon'],
    { frequency: 0.46, relevance: 0.72, diversity: 0.4, recency: 0.3, consolidation: 0.66, conceptual: 0.17, phase_boost: 0 },
    { status: 'promoted', promoted_to: 'builtin:user', promoted_ago: 22.26 }]
]

function scoreOf(c) {
  const raw = Object.entries(W).reduce((a, [k, w]) => a + w * c[k], 0) + c.phase_boost + (c.lindy || 0) + (c.inversion || 0)
  return r3(raw)
}

const candidates = RAW.map(([text, target, kind, sess, tags, quotes, comps, extra], i) => {
  const key = 'claim:' + sha(text)
  const raw = scoreOf(comps)
  const sessions = sess.map(ref)
  const days = [...new Set(sessions.map(s => s.day))].sort().reverse()
  const origin = extra.origin || 'owner'
  const signal_count = Math.max(1, Math.round(Math.expm1(comps.frequency * Math.log1p(10))))
  const status = extra.status || 'candidate'
  const passes = {
    origin: origin === 'owner' || origin === 'agent',
    placement: kind !== 'procedure',
    signals: signal_count >= 2,
    sessions: sessions.length >= 2,
    durability: comps.relevance >= 0.5,
    age: true,
    score: raw >= MIN_SCORE,
    not_promoted: status !== 'promoted',
    not_rejected: status !== 'rejected'
  }
  let missing = extra.missing
  if (!missing) {
    missing = []
    if (!passes.sessions) missing.push(`needs ${2 - sessions.length} more session (seen in ${sessions.length} of 2)`)
    if (!passes.signals) missing.push('needs 1 more mention')
    if (!passes.score) missing.push(`score ${raw.toFixed(2)} < ${MIN_SCORE.toFixed(2)}`)
    if (!passes.durability) missing.push('looks temporary (durability < 0.5)')
    if (!passes.placement) missing.unshift('a procedure: belongs in a skill, not in memory')
  }
  if (status === 'promoted' || status === 'rejected' || status === 'skill') missing = status === 'skill' ? missing : []
  const lastSeen = NOW - (1 - comps.recency) * 20 * DAY - 6 * 3600
  const firstSeen = lastSeen - (days.length * 2.5 + i % 4) * DAY
  const promotedAt = extra.promoted_ago != null ? r3(NOW - extra.promoted_ago * DAY) : null
  return {
    key, text, target, kind, origin,
    explicit: !!extra.explicit,
    pinned: false,
    score: r3(Math.min(1, raw)), raw, min_score: MIN_SCORE,
    components: Object.fromEntries(Object.entries(comps).map(([k, v]) => [k, r3(v)])),
    passes, missing, signal_count, sessions, days, tags,
    evidence: quotes.slice(0, 3).map((q, j) => ({ source_session: sess[j % sess.length], day: S[sess[j % sess.length]].day, quote: q })),
    first_seen_at: r3(firstSeen), last_seen_at: r3(lastSeen),
    status,
    promoted_to: extra.promoted_to || null,
    promoted_at: promotedAt,
    deferred_reason: extra.deferred_reason || null,
    reask: !!extra.reask
  }
})
const C = Object.fromEntries(candidates.map(c => [c.text.slice(0, 18), c]))
const byStart = s => candidates.find(c => c.text.startsWith(s))

// ---------------------------------------------------------------- budgets
const entry = (text, owner, cand, opts = {}) => ({
  text, chars: text.length, owner, key: cand ? cand.key : null,
  created_at: cand ? cand.promoted_at : opts.created_at ?? null,
  last_confirmed_at: cand ? r3(cand.last_seen_at) : null,
  stale: !!opts.stale, pointer: !!opts.pointer
})
const memEntries = [
  entry('Maya Okafor, solo indie dev. Two projects: Lanternfall, a cozy roguelike in Godot 4.4 (GDScript, no C#), and Ledgerly, an invoicing SaaS for freelancers (TypeScript). Lanternfall is the passion project; Ledgerly pays the bills, so Ledgerly bugs come first on weekdays.', 'human', null, { created_at: NOW - 60 * DAY }),
  entry(byStart('Ledgerly runs').text, 'somnia', byStart('Ledgerly runs')),
  entry('Repo layout: ~/code/lanternfall (Godot project, assets in art/, sfx in audio/) and ~/code/ledgerly (pnpm monorepo: apps/web, packages/db, packages/pdf). Never touch ~/code/archive, it holds client work under NDA.', 'human', null, { created_at: NOW - 58 * DAY }),
  entry(byStart('Maya ships').text, 'somnia', byStart('Maya ships')),
  entry(byStart('Correction: Ledgerly').text, 'somnia', byStart('Correction: Ledgerly')),
  entry(byStart('Stripe webhooks').text, 'somnia', byStart('Stripe webhooks')),
  entry('Run tests with `pnpm test` (Ledgerly, vitest + Playwright for the invoice flow) and `godot --headless -s tests/run.gd` (Lanternfall, GUT). CI runs both on every PR; a red CI blocks the Friday build.', 'human', null, { created_at: NOW - 55 * DAY }),
  entry(byStart('Lanternfall save').text, 'somnia', byStart('Lanternfall save')),
  entry(byStart('Ledgerly emails').text, 'somnia', byStart('Ledgerly emails'), { stale: true }),
  entry('Deploys: Ledgerly on Vercel, main = production, every PR gets a preview URL. Lanternfall builds are exported by hand from the Godot editor; there is no CI export yet and Maya likes it that way.', 'human', null, { created_at: NOW - 40 * DAY }),
  entry('Never commit .env files or paste keys into chat; secrets live in 1Password (vault "Ledgerly"). Stripe test keys are fine in examples, live keys never.', 'human', null, { created_at: NOW - 39 * DAY }),
  entry('Prefer small PRs, one concern each; squash-merge with a conventional-commit title. Write the PR description as a checklist of what changed and how it was tested.', 'human', null, { created_at: NOW - 33 * DAY }),
  entry('Lanternfall design pillars: cozy not grindy, light as the only resource, runs under 25 minutes, no permadeath of the lantern-keeper (only of the lantern). Push back on ideas that break a pillar.', 'human', null, { created_at: NOW - 29 * DAY }),
  entry('Somnia inbox: ~/notes/dreams — open items are facts waiting for a home in the vault.', 'somnia', null, { pointer: true, created_at: NOW - 30 * DAY })
]
const userEntries = [
  entry('Maya (she/her). Born in Lagos, lives in Lisbon since 2021. Speaks English, Portuguese and Yoruba. Former backend engineer at a fintech, went indie in 2024.', 'human', null, { created_at: NOW - 60 * DAY }),
  entry(byStart('Maya prefers casual').text, 'somnia', byStart('Maya prefers casual')),
  entry(byStart('Maya reviews code').text, 'somnia', byStart('Maya reviews code')),
  entry('Likes concise answers and code over prose; expand only when asked. Hates emoji in commit messages and marketing tone anywhere. When unsure, ask one precise question instead of guessing.', 'human', null, { created_at: NOW - 50 * DAY }),
  entry(byStart('Maya works in Lisbon').text, 'somnia', byStart('Maya works in Lisbon'), { stale: true }),
  entry('Works best in the morning; avoid suggesting deep work after 18:00. Fridays after lunch are reserved for the Lanternfall build. Takes Sundays fully off, so do not schedule reminders then.', 'human', null, { created_at: NOW - 45 * DAY }),
  entry('Learning: wants to get better at shaders and at pricing strategy. When a topic touches either, offer one short explanation of the why, then move on.', 'human', null, { created_at: NOW - 43 * DAY }),
  entry('Has mild RSI: prefers keyboard-driven tools, short commands, and avoids long mouse-heavy workflows. Uses a split keyboard and Neovim, VS Code only for Godot debugging.', 'human', null, { created_at: NOW - 44 * DAY })
]
// The pointer entry carries the promotion stamp of the vault setup.
memEntries.forEach((e, i) => (e.index = i))
userEntries.forEach((e, i) => (e.index = i))
const budget = (target, limit, entries) => {
  const used = entries.reduce((a, e) => a + e.chars, 0) + Math.max(0, entries.length - 1) * 3 // "\n§\n" separators
  return { target, used, limit, goal: Math.round(limit * 0.9), entries }
}
const budgets = [budget('memory', 2200, memEntries), budget('user', 1375, userEntries)]

// ----------------------------------------------------------------- nights
// pattern per night (oldest → newest): q = quiet, n = no-op, a = active, e = error
const PATTERN = 'aqnaqqaannqaqaqnaaqqanaqqnnnea'.split('')
const nights = []
const runs = {}
PATTERN.forEach((p, i) => {
  const daysAgo = PATTERN.length - 1 - i
  const started = NOW - daysAgo * DAY - (6 * 3600 + 12 * 60) // 03:00 UTC
  const date = dayOf(started)
  const quiet = p === 'q'
  const frag = quiet ? 0 : Math.round(18 + rnd() * 50)
  const obs = quiet ? 0 : Math.round(frag * (0.3 + rnd() * 0.25))
  const staged = quiet ? 0 : Math.round(obs * 0.6)
  const themes = quiet ? 0 : Math.round(1 + rnd() * 4)
  const truths = quiet ? 0 : Math.min(3, Math.round(rnd() * 3))
  let promoted = 0, merged = 0, deferred = 0
  if (p === 'a') { promoted = 1 + Math.round(rnd() * 1.4); merged = rnd() > 0.55 ? 1 : 0 }
  if (daysAgo === 0) { promoted = 3; merged = 2; deferred = 1 }
  if (p === 'n') deferred = rnd() > 0.6 ? 1 : 0
  nights.push({
    date, started_at: started, dry_run: false,
    fragments: frag, observations: obs, staged, themes, truths,
    promoted, merged, deferred,
    llm_calls: quiet ? 0 : Math.min(13, 3 + Math.round(frag / 8)),
    quiet, noop: p === 'n' || p === 'e', errors: p === 'e' ? 2 : 0
  })
})
// Each latest-per-day night gets its changes.
const chg = (night, cand, action, target, extra = {}) => ({
  run_at: night.started_at,
  key: cand.key,
  action, target,
  text: extra.text || cand.text,
  replaced: extra.replaced || [],
  score: cand.score,
  components: cand.components,
  sessions: cand.sessions,
  reason: extra.reason || null,
  undoable: !!extra.undoable
})
const last = nights[nights.length - 1]
runs[last.started_at] = [
  chg(last, byStart('Maya ships'), 'added', 'memory', { undoable: true }),
  chg(last, byStart('Ledgerly runs'), 'merged', 'memory', {
    undoable: true,
    replaced: ['Ledgerly uses Postgres (Neon).', 'Use Drizzle for Ledgerly migrations.']
  }),
  chg(last, byStart('Maya prefers casual'), 'superseded', 'user', {
    undoable: true,
    replaced: ['Maya prefers English for everything.']
  }),
  chg(last, byStart('Correction: Ledgerly'), 'added', 'memory', { undoable: true }),
  { run_at: last.started_at, key: null, action: 'evicted', target: 'memory',
    text: 'Lanternfall is on Godot 4.2 with the old TileMap node.',
    replaced: [], score: 0.312,
    components: { frequency: 0.2, relevance: 0.5, diversity: 0.2, recency: 0.05, consolidation: 0.4, conceptual: 0.33, phase_boost: 0 },
    sessions: [ref('s-lf-tiles')],
    reason: 'pareto tail: the least-confirmed Somnia entry (34 days), and contradicted by the TileMapLayer note',
    undoable: true },
  chg(last, byStart('Lanternfall\'s lantern'), 'deferred', 'memory', { reason: byStart('Lanternfall\'s lantern').deferred_reason })
]
// Older active nights: a few plausible changes each.
const olderPool = [byStart('Godot 4.4'), byStart('Stripe webhooks'), byStart('Maya reviews code'), byStart('Lanternfall save'), byStart('Maya works in Lisbon'), byStart('Ledgerly emails')]
let pi = 0
nights.slice(0, -1).forEach(n => {
  if (n.quiet) return
  const list = []
  for (let k = 0; k < n.promoted; k++) {
    const c = olderPool[pi++ % olderPool.length]
    list.push(chg(n, c, c.promoted_to.startsWith('vault') ? 'vault' : 'added', c.promoted_to.startsWith('vault') ? 'vault' : c.target))
  }
  for (let k = 0; k < n.merged; k++) {
    const c = olderPool[pi++ % olderPool.length]
    list.push(chg(n, c, 'merged', c.target === 'user' ? 'user' : 'memory', { replaced: ['An older, shorter version of this note.'] }))
  }
  if (n.noop) {
    const c = byStart('Lanternfall targets')
    list.push(chg(n, c, 'skipped', 'memory', { reason: n.errors ? 'consolidation call timed out (provider 504)' : 'needs 2 sessions; seen in 1' }))
  }
  runs[n.started_at] = list
})

// ----------------------------------------------------------------- themes
const tagCount = {}
const tagKeys = {}
candidates.filter(c => c.status !== 'rejected').forEach(c => c.tags.forEach(t => {
  tagCount[t] = (tagCount[t] || 0) + 1
  ;(tagKeys[t] ||= []).push(c.key)
}))
const N = candidates.length
const themeTags = Object.keys(tagCount).filter(t => tagCount[t] >= 2).sort((a, b) => tagCount[b] - tagCount[a])
const themes = themeTags.map(tag => ({ tag, count: tagCount[tag], strength: r3(Math.min(1, (2 * tagCount[tag]) / N * 1.6)), keys: tagKeys[tag] }))
const links = []
for (let i = 0; i < themeTags.length; i++) for (let j = i + 1; j < themeTags.length; j++) {
  const a = themeTags[i], b = themeTags[j]
  const w = candidates.filter(c => c.tags.includes(a) && c.tags.includes(b)).length
  if (w) links.push({ a, b, weight: w })
}

// ------------------------------------------------------------------ diary
const diary = [
  [0, `The lantern kept coming back tonight. Not the code of it, the feeling: how the dark should close in when the oil runs low, how Friday is a small ceremony of shipping. I held on to the Friday ritual, and to a quieter truth about Ledgerly: it lives on Drizzle, and Prisma is a door Maya has closed.

Somewhere between two conversations she switched to Portuguese and back to English for a commit message. I wrote that down too, so I stop answering in the wrong voice.

I wanted to keep the oil curve as well, but the room was full. Tomorrow, maybe, if something older is ready to leave.`],
  [1, `A quiet-loud night. Lots of talk about tax wording for European invoices, all of it from one long thread in a group chat, so I only looked and did not keep. The Steam Deck came up twice with the same stubborn number: sixty frames or it does not ship. Not yet twice in different rooms, though.`],
  [3, `Tiles. The old TileMap is gone and its heir, TileMapLayer, moved in. I put that in the vault inbox, where it can wait for Maya to give it a proper shelf.`],
  [4, `Webhooks that can be replayed without fear: a small, durable comfort. I added it beside the other Ledgerly truths. The rest of the night was pixel art at sixteen by sixteen, which I am still turning over.`],
  [7, `She asked for review without the sugar. Numbered, plain, honest. I can do that.`],
  [9, `Nothing new reached me tonight. I walked the old rooms and found them as I left them.`, true]
].map(([ago, text, fallback]) => ({ date: new Date((NOW - ago * DAY - 6 * 3600 - 12 * 60) * 1000).toLocaleString('en-US', { month: 'long', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC' }) + ' UTC', at: r3(NOW - ago * DAY - 6 * 3600 + 300), text, fallback: !!fallback }))

// ------------------------------------------------------------------ stats
const histogram = Array.from({ length: 20 }, (_, i) => ({ bucket: r3(i * 0.05), count: 0 }))
candidates.forEach(c => histogram[Math.min(19, Math.floor(c.score / 0.05))].count++)
const by_gate = { origin: 0, placement: 0, signals: 0, sessions: 0, durability: 0, age: 0, score: 0, not_promoted: 0, not_rejected: 0 }
candidates.filter(c => c.status === 'candidate' || c.status === 'skill').forEach(c => Object.entries(c.passes).forEach(([g, ok]) => { if (!ok) by_gate[g]++ }))
by_gate.not_promoted = candidates.filter(c => c.status === 'promoted').length
by_gate.not_rejected = candidates.filter(c => c.status === 'rejected').length

// ----------------------------------------------------------- sessions.fed
const fed = {}
// Only the last ten days of promotions, so the ✦ marks stay meaningful in the demo.
candidates.filter(c => c.status === 'promoted' && c.promoted_at > NOW - 10 * DAY).forEach(c => c.sessions.forEach(s => (fed[s.id] ||= []).push(c.key)))

// --------------------------------------------------------------- overview
const counts = {
  candidates: candidates.length,
  near: candidates.filter(c => c.status === 'candidate').length,
  promoted: candidates.filter(c => c.status === 'promoted').length,
  rejected: candidates.filter(c => c.status === 'rejected').length,
  skills: candidates.filter(c => c.kind === 'procedure' && c.status === 'skill').length,
  stale: [...memEntries, ...userEntries].filter(e => e.stale).length,
  pending: 0
}
function PRINCIPLES(active) {
  return {
    active, all: ['pareto', 'lindy', 'inversion', 'occam', 'feynman'],
    labels: {
      pareto: 'Pareto: keep the vital few', lindy: 'Lindy: trust what has lasted',
      inversion: 'Inversion: remember the mistakes', occam: 'Occam: merge only when it is simpler',
      feynman: 'Feynman: if it cannot be said plainly, wait'
    },
    descriptions: {
      pareto: 'When memory is full, the least-confirmed Somnia entries make room for stronger facts.',
      lindy: 'Facts that keep being true across weeks get a small bonus (up to +0.05).',
      inversion: 'Corrections and pitfalls get a +0.06 bonus, so the same mistake is not repeated.',
      occam: 'A merge is applied only when the result is shorter than the entries it replaces.',
      feynman: 'A rewritten entry must read as one plain sentence; otherwise the parts are kept.'
    }
  }
}
const nextRun = new Date((NOW + (17 * 3600 + 48 * 60)) * 1000).toISOString() // tonight 03:00 UTC
const overview = {
  version: '0.4.0',
  destinations: 'both',
  destructive: 'apply',
  schedule: '0 3 * * *',
  timezone: 'Europe/Lisbon',
  cron: { installed: true, id: 'cron_somnia_9f2c', next_run_at: nextRun, state: 'scheduled' },
  llm: { provider: 'openrouter', model: 'google/gemini-2.5-flash', task: 'auxiliary.somnia' },
  last_run: { at: last.started_at, dry_run: false, summary: '2 added, 1 merged, 1 deferred (memory full)' },
  budgets,
  nights,
  alarm: {
    kind: 'budget_full',
    nights: 1,
    message: `MEMORY.md is ${Math.round(budgets[0].used / budgets[0].limit * 100)}% full, so last night one fact had to wait. Review the 2 stale entries or move some to your vault.`,
    best: null,
    action: 'review'
  },
  counts,
  vault: { path: '~/notes', folder: 'dreams', inbox_notes: 4, open_items: 7 },
  pressure: { memory: r3(budgets[0].used / budgets[0].limit), user: r3(budgets[1].used / budgets[1].limit) },
  principles: PRINCIPLES(['pareto', 'lindy', 'occam', 'feynman']),
  settings: { destructive: 'apply', max_promotions: 3, min_score: MIN_SCORE, min_sessions: 2, diary: true, notify: 'changes', principles: ['pareto', 'lindy', 'occam', 'feynman'] }
}

// Noop alarm variant (harness ?alarm=noop).
const nearBest = candidates.filter(c => c.status === 'candidate').sort((a, b) => b.raw - a.raw)[0]
const noopAlarm = {
  kind: 'noop', nights: 5,
  message: `5 nights without remembering anything. The closest candidate scored ${nearBest.score.toFixed(2)} and was blocked by “${nearBest.missing[0] || 'score'}”.`,
  best: nearBest, action: 'review'
}

// Dry-run proposal (what tonight would do right now).
const dryRun = {
  summary: 'Would add 1, supersede 1, and defer 1. No files written.',
  changes: [
    { ...chg({ started_at: NOW }, byStart('Lanternfall targets'), 'added', 'memory'), score: 0.67 },
    { ...chg({ started_at: NOW }, byStart('Maya works in Lisbon'), 'superseded', 'user', {
      text: 'Maya works in Lisbon time (WET/WEST); mornings are for deep work.',
      replaced: ['Maya works in Lisbon time (WET/WEST).']
    }) },
    chg({ started_at: NOW }, byStart('Preview deploys'), 'deferred', 'memory', { reason: 'needs 1 more day of evidence (consolidation)' }),
    chg({ started_at: NOW }, byStart('Pixel art'), 'skipped', 'memory', { reason: 'feynman: the merged sentence needed three clauses to say one thing; kept the parts' })
  ]
}

const sessionsList = SESSIONS.map(s => ({ id: s.id, title: s.title, source: s.source }))

const transcript = [
  { role: 'user', text: 'what do you remember about how I ship Lanternfall?' },
  { role: 'assistant', text: 'You ship every Friday to itch.io and like short release notes in your own voice. I picked that up across three of our chats this month — here is the memory and why it was kept:' },
  { role: 'assistant', text: `::somnia-memory{key="${byStart('Maya ships').key}"}` },
  { role: 'assistant', text: 'There is also a near miss about the Steam Deck frame target that I have not kept yet:' },
  { role: 'assistant', text: `::somnia-memory{key="${byStart('Lanternfall targets').key}"}` }
]

const demo = {
  now: NOW,
  overview, runs, latest_run: last.started_at,
  candidates, themes: { themes, links },
  diary: { entries: diary, path: '~/notes/dreams/DREAMS.md' },
  stats: { histogram, min_score: MIN_SCORE, by_gate, promoted_total: counts.promoted, rejected_total: counts.rejected, candidates_total: counts.candidates },
  sessions_fed: { sessions: fed },
  dry_run: dryRun,
  noop_alarm: noopAlarm,
  sessions: sessionsList,
  transcript,
  undo_backup: {
    memory: memEntries.filter(e => e.key !== byStart('Maya ships').key).map(e => e.key === byStart('Ledgerly runs').key
      ? [{ ...e, text: 'Ledgerly uses Postgres (Neon).', chars: 30, owner: 'human', key: null }, { ...e, text: 'Use Drizzle for Ledgerly migrations.', chars: 36, owner: 'human', key: null }]
      : [e]).flat(),
    user: userEntries.map(e => e.key === byStart('Maya prefers casual').key ? { ...e, text: 'Maya prefers English for everything.', chars: 36, key: null, owner: 'human' } : e)
  }
}

const emptyNow = NOW
const empty = {
  now: emptyNow,
  overview: {
    version: '0.4.0', destinations: 'builtin', destructive: 'apply', schedule: '0 3 * * *', timezone: 'Europe/Lisbon',
    cron: { installed: true, id: 'cron_somnia_9f2c', next_run_at: nextRun, state: 'scheduled' },
    llm: { provider: 'openrouter', model: 'google/gemini-2.5-flash', task: 'auxiliary.somnia' },
    last_run: null,
    budgets: [
      budget('memory', 2200, [entry('Maya Okafor, indie dev. Two projects: Lanternfall (Godot) and Ledgerly (SaaS).', 'human', null, { created_at: NOW - 3 * DAY })].map((e, i) => ({ ...e, index: i }))),
      budget('user', 1375, [])
    ],
    nights: [], alarm: null,
    counts: { candidates: 0, near: 0, promoted: 0, rejected: 0, skills: 0, stale: 0, pending: 0 },
    vault: null, pressure: { memory: 0.04, user: 0 },
    principles: PRINCIPLES(['pareto', 'lindy', 'occam', 'feynman']),
    settings: { destructive: 'apply', max_promotions: 3, min_score: MIN_SCORE, min_sessions: 2, diary: true, notify: 'changes', principles: ['pareto', 'lindy', 'occam', 'feynman'] }
  },
  runs: {}, latest_run: null, candidates: [], themes: { themes: [], links: [] },
  diary: { entries: [], path: '~/.hermes/plugin-data/hermes-somnia/DREAMS.md' },
  stats: { histogram: histogram.map(h => ({ ...h, count: 0 })), min_score: MIN_SCORE, by_gate: Object.fromEntries(Object.keys(by_gate).map(k => [k, 0])), promoted_total: 0, rejected_total: 0, candidates_total: 0 },
  sessions_fed: { sessions: {} },
  dry_run: { summary: 'Nothing to do yet: no conversations since install.', changes: [] },
  noop_alarm: null,
  sessions: sessionsList.slice(0, 4),
  transcript: [{ role: 'user', text: 'hi!' }, { role: 'assistant', text: 'Hello Maya.' }],
  undo_backup: null
}

writeFileSync(join(here, 'demo.json'), JSON.stringify(demo, null, 1))
writeFileSync(join(here, 'empty.json'), JSON.stringify(empty, null, 1))
console.log('demo:', candidates.length, 'candidates,', nights.length, 'nights,', themes.length, 'themes; memory', budgets[0].used, '/', budgets[0].limit, 'user', budgets[1].used, '/', budgets[1].limit)
