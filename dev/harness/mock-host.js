// In-memory stand-in for the Python half (docs/rpc.md). Reads a fixture, serves
// every read method, and MUTATES the fixture on writes so flows can be exercised.
// Every call is logged to window.__rpcLog.

const DAY = 86400
const clone = v => JSON.parse(JSON.stringify(v))
const WEIGHTS = { frequency: 0.24, relevance: 0.3, diversity: 0.15, recency: 0.15, consolidation: 0.1, conceptual: 0.06 }

export function createMockServer(fixture, { mode = 'normal', latency = 90 } = {}) {
  const db = clone(fixture)
  const jobs = new Map()
  let jobSeq = 0
  const log = (window.__rpcLog = window.__rpcLog || [])
  const now = () => Date.now() / 1000

  const cand = key => {
    const c = db.candidates.find(x => x.key === key)
    if (!c) throw rpcError(4001, `KeyError: unknown candidate ${key}`)
    return c
  }
  const budget = target => {
    const b = db.overview.budgets.find(x => x.target === target)
    if (!b) throw rpcError(4001, `ValueError: unknown target ${target}`)
    return b
  }
  const reindex = b => {
    b.entries.forEach((e, i) => (e.index = i))
    b.used = b.entries.reduce((a, e) => a + e.chars, 0) + Math.max(0, b.entries.length - 1) * 3
  }
  const entryAt = (target, index, old) => {
    const b = budget(target)
    const e = b.entries[index]
    if (!e || e.text !== old) throw rpcError(4001, 'ValueError: the entry changed since the page loaded; refresh and try again')
    return { b, e }
  }

  function overview() {
    const ov = db.overview
    ov.budgets.forEach(reindex)
    const cs = db.candidates
    ov.counts = {
      candidates: cs.length,
      near: cs.filter(c => c.status === 'candidate').length,
      promoted: cs.filter(c => c.status === 'promoted').length,
      rejected: cs.filter(c => c.status === 'rejected').length,
      skills: cs.filter(c => c.status === 'skill').length,
      stale: ov.budgets.reduce((n, b) => n + b.entries.filter(e => e.stale).length, 0),
      pending: 0
    }
    ov.pressure = Object.fromEntries(ov.budgets.map(b => [b.target, Math.round((b.used / b.limit) * 1000) / 1000]))
    if (ov.alarm && ov.alarm.kind === 'budget_full') {
      const mem = ov.budgets.find(b => b.target === 'memory')
      if (mem && mem.used / mem.limit < 0.9) ov.alarm = null
    }
    return ov
  }

  function finishJob(job) {
    if (job.kind === 'dry_run') {
      job.result = clone(db.dry_run)
      job.summary = 'Dry run finished.'
    } else if (job.kind === 'promote') {
      const c = cand(job.params.key)
      const target = job.params.target || c.target
      c.status = 'promoted'
      c.promoted_to = target === 'vault' ? 'vault:dreams/today.md' : `builtin:${target}`
      c.promoted_at = now()
      if (target !== 'vault') {
        const b = budget(target)
        b.entries.push({ index: b.entries.length, text: c.text, chars: c.text.length, owner: 'somnia', key: c.key, created_at: now(), last_confirmed_at: c.last_seen_at, stale: false, pointer: false })
        reindex(b)
      }
      Object.values(db.sessions_fed.sessions)
      c.sessions.forEach(s => (db.sessions_fed.sessions[s.id] ||= []).push(c.key))
      job.summary = `Added to ${target === 'user' ? 'USER.md' : target === 'vault' ? 'the vault inbox' : 'MEMORY.md'}.`
      job.result = { changes: [{ run_at: now(), key: c.key, action: target === 'vault' ? 'vault' : 'added', target, text: c.text, replaced: [], score: c.score, components: c.components, sessions: c.sessions, reason: null, undoable: true }] }
    } else {
      // A real run on the demo data: nothing new since the last dream → a quiet night.
      job.summary = 'Quiet: no new conversations since the last dream.'
      job.result = { dry_run: false, quiet: true, fragments: 0, promoted: 0, merged: 0, deferred: 0, staged: 0, changes: [] }
    }
  }

  const handlers = {
    overview: () => overview(),
    changes: ({ run = 'latest' } = {}) => {
      const at = run === 'latest' ? db.latest_run : Number(run)
      const night = db.overview.nights.find(n => n.started_at === at)
      return { run_at: at ?? null, date: night ? night.date : null, changes: at != null ? db.runs[at] || [] : [] }
    },
    candidates: ({ filter = 'near', q, limit = 50 } = {}) => {
      let items = db.candidates
      if (filter === 'near') items = items.filter(c => c.status === 'candidate' || c.status === 'staged')
      else if (filter === 'promoted') items = items.filter(c => c.status === 'promoted')
      else if (filter === 'rejected') items = items.filter(c => c.status === 'rejected')
      else if (filter === 'skills') items = items.filter(c => c.status === 'skill')
      if (q) {
        const needle = String(q).toLowerCase()
        items = items.filter(c => c.text.toLowerCase().includes(needle) || c.tags.some(t => t.includes(needle)))
      }
      items = [...items].sort((a, b) => (filter === 'promoted' ? (b.promoted_at || 0) - (a.promoted_at || 0) : b.raw - a.raw))
      return { items: items.slice(0, limit), total: items.length }
    },
    candidate: ({ key }) => cand(key),
    themes: () => db.themes,
    diary: ({ limit = 10 } = {}) => ({ ...db.diary, entries: db.diary.entries.slice(0, limit) }),
    stats: () => {
      const hist = Array.from({ length: 20 }, (_, i) => ({ bucket: Math.round(i * 5) / 100, count: 0 }))
      db.candidates.forEach(c => hist[Math.min(19, Math.floor(c.score / 0.05))].count++)
      const min = db.overview.settings ? db.overview.settings.min_score : db.stats.min_score
      const by_gate = { ...db.stats.by_gate }
      by_gate.score = db.candidates.filter(c => (c.status === 'candidate' || c.status === 'skill') && c.score < min).length
      by_gate.not_promoted = db.candidates.filter(c => c.status === 'promoted').length
      by_gate.not_rejected = db.candidates.filter(c => c.status === 'rejected').length
      return { ...db.stats, histogram: hist, min_score: min, by_gate, candidates_total: db.candidates.length }
    },
    'sessions.fed': () => db.sessions_fed,
    job: ({ id }) => {
      const job = jobs.get(id)
      if (!job) throw rpcError(4001, `KeyError: unknown job ${id}`)
      if (job.state === 'running' && Date.now() - job.startedMs > 2600) {
        finishJob(job)
        job.state = 'done'
        job.finished_at = now()
      } else if (job.state === 'running') {
        job.summary = ['Light: reading new turns…', 'REM: looking for themes…', 'Deep: scoring candidates…'][Math.min(2, Math.floor((Date.now() - job.startedMs) / 900))]
      }
      const { startedMs, params, ...out } = job
      return out
    },
    forget: ({ key, remove_entry }) => {
      const c = cand(key)
      const wasPromoted = c.status === 'promoted'
      c.status = 'rejected'
      let removed = false
      if (remove_entry ?? wasPromoted) {
        db.overview.budgets.forEach(b => {
          const before = b.entries.length
          b.entries = b.entries.filter(e => e.key !== key)
          removed = removed || before !== b.entries.length
          reindex(b)
        })
      }
      return { ok: true, removed_entry: removed }
    },
    unforget: ({ key }) => {
      const c = cand(key)
      c.status = c.kind === 'procedure' ? 'skill' : 'candidate'
      return { ok: true }
    },
    pin: ({ key, pinned }) => {
      cand(key).pinned = !!pinned
      return { ok: true }
    },
    promote: params => {
      cand(params.key)
      return startJob('promote', params)
    },
    'entry.edit': ({ target, index, old, text }) => {
      const { b, e } = entryAt(target, index, old)
      e.text = text
      e.chars = text.length
      reindex(b)
      if (b.used > b.limit) throw rpcError(4001, 'ValueError: over the character budget')
      return { ok: true }
    },
    'entry.remove': ({ target, index, old, reject }) => {
      const { b, e } = entryAt(target, index, old)
      b.entries.splice(index, 1)
      reindex(b)
      if (reject && e.key) {
        const c = db.candidates.find(x => x.key === e.key)
        if (c) c.status = 'rejected'
      }
      return { ok: true }
    },
    'entry.to_vault': ({ target, index, old }) => {
      if (!db.overview.vault) throw rpcError(4001, 'ValueError: no vault configured')
      const { b } = entryAt(target, index, old)
      b.entries.splice(index, 1)
      reindex(b)
      db.overview.vault.open_items++
      return { ok: true }
    },
    undo: () => {
      if (!db.undo_backup) throw rpcError(4001, 'RuntimeError: nothing to undo')
      db.overview.budgets.forEach(b => {
        if (db.undo_backup[b.target]) {
          b.entries = clone(db.undo_backup[b.target])
          reindex(b)
        }
      })
      db.undo_backup = null
      ;(db.runs[db.latest_run] || []).forEach(c => (c.undoable = false))
      return { ok: true, restored: ['MEMORY.md', 'USER.md'] }
    },
    run: ({ dry_run } = {}) => startJob(dry_run ? 'dry_run' : 'run', {}),
    'skill.dismiss': ({ key }) => {
      cand(key).status = 'rejected'
      return { ok: true }
    },
    'settings.set': ({ key, value }) => {
      const allowed = ['destructive', 'max_promotions', 'min_score', 'min_sessions', 'diary', 'notify', 'principles']
      if (!allowed.includes(key)) throw rpcError(4001, `ValueError: ${key} is not an inline setting`)
      db.overview.settings[key] = value
      if (key === 'principles') db.overview.principles.active = [...value]
      if (key === 'destructive') db.overview.destructive = value
      if (key === 'min_score') db.candidates.forEach(c => (c.min_score = value))
      return { ok: true, key, value }
    }
  }

  function startJob(kind, params) {
    const running = [...jobs.values()].find(j => j.state === 'running')
    if (running) return { ok: true, job: running.id, already_running: true }
    const id = `job_${++jobSeq}`
    jobs.set(id, { id, kind, state: 'running', started_at: now(), finished_at: null, summary: 'Starting…', result: null, error: null, startedMs: Date.now(), params })
    return { ok: true, job: id }
  }

  return {
    db,
    request(method, params = {}) {
      const entry = { method, params: clone(params || {}), at: Date.now() }
      log.push(entry)
      return new Promise((resolve, reject) => {
        setTimeout(() => {
          try {
            if (mode === 'disabled') throw rpcError(-32601, `Method not found: ${method}`)
            const name = method.replace(/^plugin\.hermes-somnia\./, '')
            const fn = handlers[name]
            if (!fn || !method.startsWith('plugin.hermes-somnia.')) throw rpcError(-32601, `Method not found: ${method}`)
            const out = clone(fn(params || {}))
            entry.ok = true
            resolve(out)
          } catch (err) {
            entry.ok = false
            entry.error = err.message
            reject(err)
          }
        }, latency)
      })
    }
  }
}

function rpcError(code, message) {
  const err = new Error(message)
  err.name = 'JsonRpcGatewayError'
  err.code = code
  if (code === 4001) err.data = { reason: 'plugin_rpc_failed', detail: message }
  return err
}

export { WEIGHTS, DAY }
