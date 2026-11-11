/**
 * Somnia — the desktop half.
 *
 * One uncompiled ESM file (no JSX, three imports). A full page at /somnia plus
 * small surfaces elsewhere in the app. Every element answers a question or
 * leads to an action:
 *
 *   "What did it learn last night, and why?"   → This morning (changes + signal breakdown)
 *   "Is it working?"                            → header state line, alarm, sleep chart
 *   "What is about to be remembered?"           → Almost there (near misses, missing gates)
 *   "How full is my memory?"                    → Memory, as space (capacity bars per entry)
 *   "Undo / forget this."                       → actions on every card, Undo last write
 *
 * Data comes only from the Python half's RPCs (docs/rpc.md), wire name
 * `plugin.hermes-somnia.<method>`. Long operations (run, dry run, promote)
 * return a job id that is polled every 2 s.
 *
 * Conventions
 *  1. `import * as sdk` and feature-detect everything optional, so an older
 *     Desktop degrades instead of crashing at load.
 *  2. No hardcoded colours: var(--ui-*) and color-mix() of them only.
 *  3. Disk plugins are not scanned by the app's Tailwind build, so this file
 *     ships its own small stylesheet (class prefix `smn-`), injected in
 *     register() and removed on dispose. SDK components bring their own styles.
 *  4. Charts are inline SVG. No timers outside React effects / ctx lifetimes.
 *
 * Sections (search for "// ===="):
 *   constants · sdk · i18n · stores · rpc + data hooks · formatting ·
 *   primitives · score visuals · header · alarm · this morning · memory space ·
 *   almost there · sleep chart · themes · diary · calibration · principles ·
 *   dialogs · page · surfaces · styles · register
 */

import * as sdk from '@hermes/plugin-sdk'
import {
  Fragment,
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore
} from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// ==== constants =============================================================

const PLUGIN_ID = 'hermes-somnia'
const ROUTE = '/somnia'
const RPC_PREFIX = `plugin.${PLUGIN_ID}.`
const QK = PLUGIN_ID // React Query key root: ['hermes-somnia', …]
const JOB_POLL_MS = 2000
const OVERVIEW_POLL_MS = 60 * 1000
const DAY_S = 86400

// The six Deep signals, in display order, with their weights (docs/rpc.md `Weights`).
const SIGNALS = [
  ['relevance', 0.3],
  ['frequency', 0.24],
  ['diversity', 0.15],
  ['recency', 0.15],
  ['consolidation', 0.1],
  ['conceptual', 0.06]
]
// Additive bonuses that may ride on `components`.
const BONUSES = ['phase_boost', 'lindy', 'inversion']
const PRINCIPLES = ['pareto', 'lindy', 'inversion', 'occam', 'feynman']
// Off unless the user turns them on (contract: default active = pareto, lindy, occam, feynman).
const OPT_IN_PRINCIPLES = ['inversion']
const PRINCIPLE_ICON = {
  pareto: 'graph',
  lindy: 'history',
  inversion: 'arrow-swap',
  occam: 'fold',
  feynman: 'lightbulb'
}

// Colour role per change action / candidate state (all theme vars).
const ACTION_COLOR = {
  added: 'var(--ui-green)',
  merged: 'var(--ui-accent)',
  superseded: 'var(--ui-purple)',
  staged: 'var(--ui-yellow)',
  vault: 'var(--ui-cyan)',
  deferred: 'var(--ui-orange)',
  skipped: 'var(--ui-text-tertiary)',
  evicted: 'var(--ui-red)'
}
const ACTION_ICON = {
  added: 'add',
  merged: 'merge',
  superseded: 'replace',
  staged: 'inbox',
  vault: 'file-symlink-file',
  deferred: 'clock',
  skipped: 'circle-slash',
  evicted: 'remove'
}

// ==== sdk ===================================================================

const host = sdk.host || {}
const {
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  SearchField,
  SegmentedControl,
  Skeleton,
  Streamdown,
  Switch,
  Textarea,
  Tip,
  useMutation,
  useQuery,
  useQueryClient,
  useValue,
  usePluginI18n,
  relativeTime,
  pluginSettingsHref,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  SIDEBAR_NAV_PREFS_AREA,
  PALETTE_AREA,
  KEYBINDS_AREA,
  STATUSBAR_AREAS,
  TRANSCRIPT_DIRECTIVE_AREA,
  SESSION_ROW_AREAS,
  CHAT_EMPTY_AREA
} = sdk

// ==== i18n ==================================================================
// Same lookup as Desktop's ctx.i18n: dotted key path in the active language,
// then English, then the key. Functions receive the call's arguments.

const plural = (n, one, many) => (n === 1 ? one : many)

const EN = {
  name: 'Somnia',
  open: { palette: 'Somnia: Open dream journal', keybind: 'Open Somnia' },
  settingsPage: { intro: 'Somnia rereads your conversations every night and keeps what keeps coming back. The rules it follows are below; the full journal is on its own page.', open: 'Open Somnia' },
  common: {
    cancel: 'Cancel',
    close: 'Close',
    save: 'Save',
    retry: 'Retry',
    showMore: n => `Show ${n} more`,
    showLess: 'Show less',
    working: 'Working…',
    chars: n => `${n.toLocaleString()} chars`
  },
  header: {
    tagline: 'Every night it re-reads your chats and keeps what keeps coming back.',
    lastDream: ago => `Last dream ${ago}`,
    neverDreamed: 'Has not dreamed yet',
    next: when => `next ${when}`,
    noSchedule: 'no nightly schedule',
    writesTo: where => `writes to ${where}`,
    dest: { builtin: 'MEMORY.md + USER.md', vault: 'vault inbox', both: 'MEMORY.md + vault' },
    stage: 'merges wait for your review',
    model: m => `Model: ${m}`,
    dreamNow: 'Dream now',
    dreaming: 'Dreaming…',
    preview: 'Preview',
    previewHint: 'Dry run: see what tonight would change. Writes nothing.',
    undo: 'Undo last write',
    undoHint: 'Restore MEMORY.md / USER.md from the backup taken before the last write',
    settings: 'Settings',
    running: (label, s) => `${label} · ${s}s`,
    jobRun: 'Dreaming',
    jobDry: 'Previewing tonight',
    jobPromote: 'Writing it now'
  },
  time: {
    tonight: hm => `tonight ${hm}`,
    today: hm => `today ${hm}`,
    tomorrow: hm => `tomorrow ${hm}`,
    on: (d, hm) => `${d} ${hm}`
  },
  alarm: {
    title: {
      noop: n => `${n} ${plural(n, 'night', 'nights')} without remembering anything`,
      stale_cron: 'The nightly dream did not run',
      errors: n => `${n} ${plural(n, 'night', 'nights')} with errors`,
      budget_full: 'Memory is nearly full'
    },
    closest: 'Closest candidate',
    action: { run: 'Dream now', settings: 'Open settings', review: 'Review' }
  },
  morning: {
    title: 'This morning',
    titleOlder: date => `Last dream · ${date}`,
    titleNight: date => `Night of ${date}`,
    back: 'Back to latest',
    hint: 'What changed in memory, and why each one made it.',
    kept: n => `${n} kept out of sight`,
    showKept: 'Show',
    nothing: 'Nothing changed that night.',
    nothingHint: 'It looked, but no candidate passed every gate. See “Almost there” for the closest ones.',
    neverTitle: 'Somnia hasn’t dreamed yet',
    neverBody: when => `It will dream ${when}, or you can start now. The first night reads your recent chats and only keeps what came up more than once.`,
    neverNoCron: 'No nightly schedule is installed yet. Run `hermes somnia setup`, or start a dream now.',
    replaced: n => `replaced ${n} ${plural(n, 'entry', 'entries')}`,
    from: 'From',
    action: {
      added: 'Added',
      merged: 'Merged',
      superseded: 'Superseded',
      staged: 'Staged',
      vault: 'To vault',
      deferred: 'Deferred',
      skipped: 'Skipped',
      evicted: 'Evicted'
    },
    target: { memory: 'MEMORY.md', user: 'USER.md', vault: 'vault inbox' },
    evictedNote: 'Removed to make room. Only entries Somnia wrote itself are ever evicted.',
    stagedNote: 'Waiting for you in /memory pending.',
    keep: 'Keep',
    keepHint: 'Looks right. Hide this card.',
    forget: 'Forget',
    edit: 'Edit',
    toVault: 'To vault',
    ask: 'Ask Hermes',
    promote: 'Promote now',
    pin: 'Pin',
    unpin: 'Unpin',
    pinHint: 'Count it as something you asked to remember: skips the recurrence gates next night.'
  },
  signals: {
    title: 'Why it scored',
    total: 'Score',
    min: m => `needs ${m}`,
    names: {
      relevance: 'Durability',
      frequency: 'Frequency',
      diversity: 'Sessions',
      recency: 'Recency',
      consolidation: 'Across days',
      conceptual: 'Themes',
      phase_boost: 'Light + REM',
      lindy: 'Lindy',
      inversion: 'Inversion'
    },
    help: {
      relevance: 'How much it reads like a lasting fact rather than a passing detail. Weighted most (30%).',
      frequency: 'How often it came up. Each new mention counts less than the last (24%).',
      diversity: 'How many different conversations it appeared in (15%).',
      recency: 'How recently it was last seen. Fades over about two weeks (15%).',
      consolidation: 'Whether it came back on different days, not in one burst (10%).',
      conceptual: 'How strongly it ties into your recurring themes (6%).',
      phase_boost: 'Small bonus when both the Light and REM phases noticed it.',
      lindy: 'Lindy principle: it has stayed true for weeks, so it gets a small bonus.',
      inversion: 'Inversion principle: it is a correction or pitfall, so it gets a bonus to avoid repeating the mistake.'
    },
    contribution: (w, v, c) => `weight ${w} × value ${v} = +${c}`
  },
  space: {
    title: 'Memory, as space',
    hint: 'Every entry Hermes carries into each chat, sized by what it costs.',
    used: (u, l) => `${u.toLocaleString()} / ${l.toLocaleString()}`,
    goal: 'goal',
    legend: { somnia: 'Somnia', human: 'You', stale: 'Stale', pointer: 'Vault pointer', free: 'Free' },
    pressure: (file, pct) => `${file} is ${pct}% full. New facts will wait, or push out Somnia’s least-confirmed entries.`,
    reviewStale: n => `Review ${n} stale`,
    pick: 'Select a block to see the entry.',
    owner: 'Written by',
    ownerSomnia: 'Somnia (a dream)',
    ownerHuman: 'You or Hermes, in a chat',
    created: 'Created',
    confirmed: 'Last seen again',
    size: 'Size',
    never: 'never',
    stale: 'Not seen again for a while: it may no longer be true.',
    pointer: 'Points Hermes at your vault inbox. Removing it hides the inbox from chats.',
    edit: 'Edit',
    remove: 'Remove',
    removeHint: 'Delete the entry. Somnia may learn it again if it keeps coming up.',
    forget: 'Forget',
    forgetHint: 'Delete it and never re-learn it from the same words.',
    toVault: 'Move to vault',
    tooLong: n => `${n} chars over the limit`,
    left: n => `${n} chars left`
  },
  near: {
    title: 'Almost there',
    hint: 'What could be remembered next, and what each one still needs.',
    tabs: { near: 'Near', promoted: 'Promoted', rejected: 'Forgotten', skills: 'Skill ideas' },
    search: 'Search candidates',
    theme: tag => `Theme: ${tag}`,
    clearTheme: 'Clear theme filter',
    empty: {
      near: 'Nothing is close yet. Candidates show up here after nights with new conversations.',
      promoted: 'Nothing has been promoted yet.',
      rejected: 'Nothing forgotten. Forgotten facts are never learned again from the same words.',
      skills: 'No skill ideas yet. When the same steps keep coming up, they appear here.'
    },
    noMatch: 'No candidate matches.',
    min: m => `min ${m}`,
    passes: 'Passes every gate. It should be kept tonight.',
    evidence: 'In your words',
    sessions: n => `${n} ${plural(n, 'session', 'sessions')}`,
    days: n => `${n} ${plural(n, 'day', 'days')}`,
    seen: ago => `seen ${ago}`,
    promotedTo: (where, ago) => `in ${where} · ${ago}`,
    promote: 'Promote now',
    pin: 'Pin',
    unpin: 'Unpin',
    forget: 'Forget',
    unforget: 'Unforget',
    makeSkill: 'Turn into a skill',
    dismiss: 'Dismiss',
    backAgain: 'back again',
    backAgainHint: 'You forgot this once, but it kept coming up, so it is being offered again.',
    pinned: 'pinned',
    explicit: 'you asked',
    kind: { fact: 'fact', preference: 'preference', rule: 'rule', procedure: 'procedure', correction: 'correction' },
    untrusted: 'group chat'
  },
  chart: {
    title: 'Sleep chart',
    hint: 'The last 30 nights. Deeper means something was kept.',
    stages: { awake: 'Quiet', light: 'Light', rem: 'REM', deep: 'Deep' },
    empty: 'No nights yet.',
    summary: (kept, quiet, noop) => `${kept} kept · ${quiet} quiet · ${noop} without result`,
    tip: {
      quiet: 'Quiet: no new conversations',
      light: (f, o) => `${f} fragments read · ${o} observations`,
      rem: (th, tr) => `${th} themes · ${tr} possible truths`,
      deep: (p, m, d) => `${p} kept · ${m} merged · ${d} deferred`,
      noop: 'Had material, kept nothing',
      errors: n => `${n} ${plural(n, 'error', 'errors')}`,
      dry: 'dry run',
      click: 'Click to see that night'
    }
  },
  themes: {
    title: 'Recurring themes',
    hint: 'Concepts that keep returning. Click one to filter candidates.',
    empty: 'Themes appear once two facts share a concept.',
    count: n => `${n} ${plural(n, 'fact', 'facts')}`
  },
  diary: {
    title: 'Dream diary',
    hint: 'Written by Somnia after each night.',
    empty: 'The diary starts after the first night.',
    fallback: 'Written without the model this night (fallback).'
  },
  calib: {
    title: 'Are my gates too strict?',
    hint: n => `${n} candidates by score. Everything right of the line can be kept.`,
    blockedBy: 'Blocked by',
    gates: {
      origin: 'untrusted source',
      placement: 'belongs in a skill',
      signals: 'too few mentions',
      sessions: 'too few sessions',
      durability: 'looks temporary',
      age: 'too new',
      score: 'score below the line',
      not_promoted: 'already kept',
      not_rejected: 'forgotten'
    },
    minScore: 'Minimum score',
    minSessions: 'Minimum sessions'
  },
  thinks: {
    title: 'How Somnia thinks',
    hint: 'Rules of thumb applied every night.',
    destructive: 'Merges and removals',
    apply: 'Apply',
    stage: 'Ask me first',
    maxPromotions: 'Max new facts per night',
    optIn: 'opt-in',
    optInHint: 'Off by default: it showed no measurable gain in the benchmark.',
    saved: 'Saved. Applies from the next dream.'
  },
  preview: {
    title: 'What tonight would change',
    none: 'Nothing would change right now.',
    run: 'Dream now for real'
  },
  confirm: {
    forgetTitle: 'Forget this forever?',
    forgetBody: 'The entry is removed from memory and Somnia will not learn it again from the same words (unless it keeps coming back for months).',
    forgetLabel: 'Forget',
    undoTitle: 'Undo the last write?',
    undoBody: 'MEMORY.md and USER.md go back to the backup taken right before the last dream wrote to them. If you edited them since, the undo is refused.',
    undoLabel: 'Undo',
    removeTitle: 'Remove this entry?',
    removeBody: 'It disappears from the memory Hermes reads in every chat.',
    removeLabel: 'Remove'
  },
  toast: {
    forgotten: 'Forgotten. It will not come back from the same words.',
    unforgotten: 'Unforgotten. It can be learned again.',
    pinned: 'Pinned. It counts as something you asked to remember.',
    unpinned: 'Unpinned.',
    edited: 'Memory entry updated.',
    removed: 'Entry removed.',
    moved: 'Moved to the vault inbox.',
    undone: files => `Restored ${files}.`,
    dismissed: 'Dismissed.',
    jobDone: 'Dream finished',
    jobFailed: 'Dream failed',
    askFailed: 'Open a new chat to ask about it. The question was not placed.',
    alreadyRunning: 'A dream is already running.'
  },
  ask: {
    memory: (text) => `Why do you remember this, and is it still true?\n\n> ${text}`,
    skill: (text, quotes) => `This keeps coming up in our chats. Please turn it into a reusable Hermes skill (a SKILL.md with clear steps):\n\n> ${text}${quotes ? `\n\nIn my words:\n${quotes}` : ''}`
  },
  disabled: {
    title: 'Somnia’s Python half is not enabled',
    body: 'This page reads its data from the plugin’s gateway methods. Enable the plugin, restart the gateway, then retry:',
    command: 'hermes plugins enable hermes-somnia'
  },
  gateway: { waiting: 'Waiting for the gateway…' },
  error: { load: 'Could not load Somnia.' },
  surfaces: {
    statusNew: n => `${n} new`,
    statusTip: n => `Somnia remembered ${n} ${plural(n, 'thing', 'things')} last night`,
    fed: n => `Fed ${n} ${plural(n, 'memory', 'memories')}`,
    emptyChat: 'Last night I remembered:',
    open: 'Open Somnia',
    card: 'Somnia memory',
    reallyForget: 'Really forget?',
    gone: 'Forgotten. It will not come back from the same words.',
    missing: 'This memory no longer exists.'
  }
}

const PT = {
  name: 'Somnia',
  open: { palette: 'Somnia: Abrir diário de sonhos', keybind: 'Abrir Somnia' },
  settingsPage: { intro: 'Toda noite o Somnia relê suas conversas e guarda o que continua voltando. As regras que ele segue estão abaixo; o diário completo fica na página dele.', open: 'Abrir o Somnia' },
  common: {
    cancel: 'Cancelar',
    close: 'Fechar',
    save: 'Salvar',
    retry: 'Tentar de novo',
    showMore: n => `Mostrar mais ${n}`,
    showLess: 'Mostrar menos',
    working: 'Trabalhando…',
    chars: n => `${n.toLocaleString('pt-BR')} caracteres`
  },
  header: {
    tagline: 'Toda noite relê suas conversas e guarda o que continua voltando.',
    lastDream: ago => `Último sonho ${ago}`,
    neverDreamed: 'Ainda não sonhou',
    next: when => `próximo ${when}`,
    noSchedule: 'sem agendamento noturno',
    writesTo: where => `escreve em ${where}`,
    dest: { builtin: 'MEMORY.md + USER.md', vault: 'caixa de entrada do vault', both: 'MEMORY.md + vault' },
    stage: 'fusões esperam sua revisão',
    model: m => `Modelo: ${m}`,
    dreamNow: 'Sonhar agora',
    dreaming: 'Sonhando…',
    preview: 'Prévia',
    previewHint: 'Simulação: veja o que esta noite mudaria. Não grava nada.',
    undo: 'Desfazer última escrita',
    undoHint: 'Restaura MEMORY.md / USER.md do backup feito antes da última escrita',
    settings: 'Configurações',
    running: (label, s) => `${label} · ${s}s`,
    jobRun: 'Sonhando',
    jobDry: 'Simulando esta noite',
    jobPromote: 'Gravando agora'
  },
  time: {
    tonight: hm => `esta noite ${hm}`,
    today: hm => `hoje ${hm}`,
    tomorrow: hm => `amanhã ${hm}`,
    on: (d, hm) => `${d} ${hm}`
  },
  alarm: {
    title: {
      noop: n => `${n} ${plural(n, 'noite', 'noites')} sem lembrar nada`,
      stale_cron: 'O sonho noturno não rodou',
      errors: n => `${n} ${plural(n, 'noite', 'noites')} com erros`,
      budget_full: 'A memória está quase cheia'
    },
    closest: 'Candidato mais próximo',
    action: { run: 'Sonhar agora', settings: 'Abrir configurações', review: 'Revisar' }
  },
  morning: {
    title: 'Esta manhã',
    titleOlder: date => `Último sonho · ${date}`,
    titleNight: date => `Noite de ${date}`,
    back: 'Voltar ao mais recente',
    hint: 'O que mudou na memória, e por que cada item entrou.',
    kept: n => `${n} ${plural(n, 'oculto', 'ocultos')}`,
    showKept: 'Mostrar',
    nothing: 'Nada mudou nesta noite.',
    nothingHint: 'Ele olhou, mas nenhum candidato passou em todos os portões. Veja “Quase lá”.',
    neverTitle: 'Somnia ainda não sonhou',
    neverBody: when => `Vai sonhar ${when}, ou você pode começar agora. A primeira noite lê suas conversas recentes e só guarda o que apareceu mais de uma vez.`,
    neverNoCron: 'Ainda não há agendamento noturno. Rode `hermes somnia setup`, ou sonhe agora.',
    replaced: n => `substituiu ${n} ${plural(n, 'entrada', 'entradas')}`,
    from: 'De',
    action: {
      added: 'Adicionado',
      merged: 'Fundido',
      superseded: 'Substituído',
      staged: 'Em revisão',
      vault: 'Para o vault',
      deferred: 'Adiado',
      skipped: 'Ignorado',
      evicted: 'Removido'
    },
    target: { memory: 'MEMORY.md', user: 'USER.md', vault: 'inbox do vault' },
    evictedNote: 'Removido para abrir espaço. Só entradas escritas pelo próprio Somnia são removidas.',
    stagedNote: 'Esperando por você em /memory pending.',
    keep: 'Manter',
    keepHint: 'Está certo. Esconder este cartão.',
    forget: 'Esquecer',
    edit: 'Editar',
    toVault: 'Para o vault',
    ask: 'Perguntar ao Hermes',
    promote: 'Promover agora',
    pin: 'Fixar',
    unpin: 'Desafixar',
    pinHint: 'Conta como algo que você pediu para lembrar: pula os portões de recorrência na próxima noite.'
  },
  signals: {
    title: 'Por que pontuou',
    total: 'Nota',
    min: m => `precisa de ${m}`,
    names: {
      relevance: 'Durabilidade',
      frequency: 'Frequência',
      diversity: 'Sessões',
      recency: 'Recência',
      consolidation: 'Em vários dias',
      conceptual: 'Temas',
      phase_boost: 'Light + REM',
      lindy: 'Lindy',
      inversion: 'Inversão'
    },
    help: {
      relevance: 'O quanto parece um fato duradouro e não um detalhe passageiro. Maior peso (30%).',
      frequency: 'Quantas vezes apareceu. Cada nova menção vale menos que a anterior (24%).',
      diversity: 'Em quantas conversas diferentes apareceu (15%).',
      recency: 'Quão recentemente foi visto. Some em cerca de duas semanas (15%).',
      consolidation: 'Se voltou em dias diferentes, e não numa rajada só (10%).',
      conceptual: 'O quanto se liga aos seus temas recorrentes (6%).',
      phase_boost: 'Pequeno bônus quando as fases Light e REM o notaram.',
      lindy: 'Princípio de Lindy: continua verdadeiro há semanas, então ganha um pequeno bônus.',
      inversion: 'Princípio da inversão: é uma correção ou armadilha, então ganha bônus para não repetir o erro.'
    },
    contribution: (w, v, c) => `peso ${w} × valor ${v} = +${c}`
  },
  space: {
    title: 'Memória, como espaço',
    hint: 'Cada entrada que o Hermes leva para toda conversa, no tamanho do que custa.',
    goal: 'meta',
    legend: { somnia: 'Somnia', human: 'Você', stale: 'Desatualizada', pointer: 'Ponteiro do vault', free: 'Livre' },
    pressure: (file, pct) => `${file} está ${pct}% cheio. Novos fatos vão esperar, ou empurrar para fora as entradas menos confirmadas do Somnia.`,
    reviewStale: n => `Revisar ${n} ${plural(n, 'desatualizada', 'desatualizadas')}`,
    pick: 'Selecione um bloco para ver a entrada.',
    owner: 'Escrita por',
    ownerSomnia: 'Somnia (um sonho)',
    ownerHuman: 'Você ou o Hermes, numa conversa',
    created: 'Criada',
    confirmed: 'Vista de novo',
    size: 'Tamanho',
    never: 'nunca',
    stale: 'Não foi vista de novo há um tempo: pode não ser mais verdade.',
    pointer: 'Aponta o Hermes para o inbox do vault. Removê-la esconde o inbox das conversas.',
    edit: 'Editar',
    remove: 'Remover',
    removeHint: 'Apaga a entrada. O Somnia pode aprendê-la de novo se continuar aparecendo.',
    forget: 'Esquecer',
    forgetHint: 'Apaga e nunca reaprende com as mesmas palavras.',
    toVault: 'Mover para o vault',
    tooLong: n => `${n} caracteres acima do limite`,
    left: n => `${n} caracteres livres`
  },
  near: {
    title: 'Quase lá',
    hint: 'O que pode ser lembrado em seguida, e o que ainda falta para cada um.',
    tabs: { near: 'Perto', promoted: 'Promovidos', rejected: 'Esquecidos', skills: 'Ideias de skill' },
    search: 'Buscar candidatos',
    theme: tag => `Tema: ${tag}`,
    clearTheme: 'Limpar filtro de tema',
    empty: {
      near: 'Nada perto ainda. Candidatos aparecem aqui depois de noites com conversas novas.',
      promoted: 'Nada foi promovido ainda.',
      rejected: 'Nada esquecido. Fatos esquecidos nunca são reaprendidos com as mesmas palavras.',
      skills: 'Nenhuma ideia de skill ainda. Quando os mesmos passos se repetem, aparecem aqui.'
    },
    noMatch: 'Nenhum candidato corresponde.',
    min: m => `mín ${m}`,
    passes: 'Passa em todos os portões. Deve ser guardado esta noite.',
    evidence: 'Nas suas palavras',
    sessions: n => `${n} ${plural(n, 'sessão', 'sessões')}`,
    days: n => `${n} ${plural(n, 'dia', 'dias')}`,
    seen: ago => `visto ${ago}`,
    promotedTo: (where, ago) => `em ${where} · ${ago}`,
    promote: 'Promover agora',
    pin: 'Fixar',
    unpin: 'Desafixar',
    forget: 'Esquecer',
    unforget: 'Desesquecer',
    makeSkill: 'Transformar em skill',
    dismiss: 'Dispensar',
    backAgain: 'voltou',
    backAgainHint: 'Você esqueceu isto uma vez, mas continuou aparecendo, então está sendo oferecido de novo.',
    pinned: 'fixado',
    explicit: 'você pediu',
    kind: { fact: 'fato', preference: 'preferência', rule: 'regra', procedure: 'procedimento', correction: 'correção' },
    untrusted: 'chat em grupo'
  },
  chart: {
    title: 'Gráfico do sono',
    hint: 'As últimas 30 noites. Mais fundo significa que algo foi guardado.',
    stages: { awake: 'Quieta', light: 'Light', rem: 'REM', deep: 'Deep' },
    empty: 'Nenhuma noite ainda.',
    summary: (kept, quiet, noop) => `${kept} guardados · ${quiet} quietas · ${noop} sem resultado`,
    tip: {
      quiet: 'Quieta: nenhuma conversa nova',
      light: (f, o) => `${f} fragmentos lidos · ${o} observações`,
      rem: (th, tr) => `${th} temas · ${tr} possíveis verdades`,
      deep: (p, m, d) => `${p} guardados · ${m} fundidos · ${d} adiados`,
      noop: 'Tinha material, não guardou nada',
      errors: n => `${n} ${plural(n, 'erro', 'erros')}`,
      dry: 'simulação',
      click: 'Clique para ver esta noite'
    }
  },
  themes: {
    title: 'Temas recorrentes',
    hint: 'Conceitos que continuam voltando. Clique para filtrar candidatos.',
    empty: 'Temas aparecem quando dois fatos compartilham um conceito.',
    count: n => `${n} ${plural(n, 'fato', 'fatos')}`
  },
  diary: {
    title: 'Diário de sonhos',
    hint: 'Escrito pelo Somnia depois de cada noite.',
    empty: 'O diário começa depois da primeira noite.',
    fallback: 'Escrito sem o modelo nesta noite (fallback).'
  },
  calib: {
    title: 'Meus portões estão rígidos demais?',
    hint: n => `${n} candidatos por nota. Tudo à direita da linha pode ser guardado.`,
    blockedBy: 'Bloqueados por',
    gates: {
      origin: 'fonte não confiável',
      placement: 'pertence a uma skill',
      signals: 'poucas menções',
      sessions: 'poucas sessões',
      durability: 'parece temporário',
      age: 'novo demais',
      score: 'nota abaixo da linha',
      not_promoted: 'já guardado',
      not_rejected: 'esquecido'
    },
    minScore: 'Nota mínima',
    minSessions: 'Sessões mínimas'
  },
  thinks: {
    title: 'Como o Somnia pensa',
    hint: 'Regras práticas aplicadas toda noite.',
    destructive: 'Fusões e remoções',
    apply: 'Aplicar',
    stage: 'Perguntar antes',
    maxPromotions: 'Máx. de fatos novos por noite',
    optIn: 'opcional',
    optInHint: 'Desligado por padrão: não mostrou ganho mensurável no benchmark.',
    saved: 'Salvo. Vale a partir do próximo sonho.'
  },
  principle: {
    pareto: { label: 'Pareto: guarde os poucos vitais', description: 'Com a memória cheia, as entradas menos confirmadas do Somnia abrem espaço para fatos mais fortes.' },
    lindy: { label: 'Lindy: confie no que durou', description: 'Fatos que continuam verdadeiros por semanas ganham um pequeno bônus (até +0,05).' },
    inversion: { label: 'Inversão: lembre dos erros', description: 'Correções e armadilhas ganham +0,06, para o mesmo erro não se repetir.' },
    occam: { label: 'Occam: funda só quando simplifica', description: 'Uma fusão só é aplicada quando o resultado é mais curto que as entradas que substitui.' },
    feynman: { label: 'Feynman: se não dá para dizer simples, espere', description: 'Uma entrada reescrita precisa caber numa frase simples; senão as partes são mantidas.' }
  },
  preview: {
    title: 'O que esta noite mudaria',
    none: 'Nada mudaria agora.',
    run: 'Sonhar agora de verdade'
  },
  confirm: {
    forgetTitle: 'Esquecer para sempre?',
    forgetBody: 'A entrada sai da memória e o Somnia não vai aprendê-la de novo com as mesmas palavras (a menos que continue voltando por meses).',
    forgetLabel: 'Esquecer',
    undoTitle: 'Desfazer a última escrita?',
    undoBody: 'MEMORY.md e USER.md voltam ao backup feito logo antes do último sonho escrever neles. Se você os editou depois, o desfazer é recusado.',
    undoLabel: 'Desfazer',
    removeTitle: 'Remover esta entrada?',
    removeBody: 'Ela some da memória que o Hermes lê em toda conversa.',
    removeLabel: 'Remover'
  },
  toast: {
    forgotten: 'Esquecido. Não volta com as mesmas palavras.',
    unforgotten: 'Desesquecido. Pode ser aprendido de novo.',
    pinned: 'Fixado. Conta como algo que você pediu para lembrar.',
    unpinned: 'Desafixado.',
    edited: 'Entrada da memória atualizada.',
    removed: 'Entrada removida.',
    moved: 'Movido para o inbox do vault.',
    undone: files => `${files} restaurado.`,
    dismissed: 'Dispensado.',
    jobDone: 'Sonho concluído',
    jobFailed: 'O sonho falhou',
    askFailed: 'Abra uma conversa nova para perguntar. A pergunta não foi colocada.',
    alreadyRunning: 'Um sonho já está rodando.'
  },
  ask: {
    memory: text => `Por que você lembra disto, e ainda é verdade?\n\n> ${text}`,
    skill: (text, quotes) => `Isto continua aparecendo nas nossas conversas. Transforme numa skill reutilizável do Hermes (um SKILL.md com passos claros):\n\n> ${text}${quotes ? `\n\nNas minhas palavras:\n${quotes}` : ''}`
  },
  disabled: {
    title: 'A parte Python do Somnia não está ativada',
    body: 'Esta página lê os dados pelos métodos do plugin no gateway. Ative o plugin, reinicie o gateway e tente de novo:',
    command: 'hermes plugins enable hermes-somnia'
  },
  gateway: { waiting: 'Esperando o gateway…' },
  error: { load: 'Não foi possível carregar o Somnia.' },
  surfaces: {
    statusNew: n => `${n} ${plural(n, 'novo', 'novos')}`,
    statusTip: n => `O Somnia lembrou ${n} ${plural(n, 'coisa', 'coisas')} esta noite`,
    fed: n => `Alimentou ${n} ${plural(n, 'memória', 'memórias')}`,
    emptyChat: 'Esta noite eu lembrei:',
    open: 'Abrir Somnia',
    card: 'Memória do Somnia',
    reallyForget: 'Esquecer mesmo?',
    gone: 'Esquecido. Não volta com as mesmas palavras.',
    missing: 'Esta memória não existe mais.'
  }
}

const LOCALES = { en: EN, pt: PT, 'pt-br': PT, 'pt-BR': PT }

let i18n = null
let storage = null

function lookup(table, key) {
  return String(key)
    .split('.')
    .reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), table)
}

function englishText(key, args) {
  const value = lookup(EN, key)
  if (typeof value === 'function') return value(...args)
  return typeof value === 'string' ? value : key
}

/** Outside React: reads the active language at call time. */
function tr(key, ...args) {
  return i18n ? i18n.t(key, ...args) : englishText(key, args)
}

/** In components: the SDK hook re-renders on a language switch. */
function useTr() {
  const t = typeof usePluginI18n === 'function' ? usePluginI18n(PLUGIN_ID) : null
  return t && i18n ? t : tr
}

// ==== stores ================================================================
// Tiny external stores (module scope, reset on dispose). They let state outlive
// a page unmount (a running job) and let surfaces talk (chat card → page focus).

function createStore(initial) {
  let value = initial
  const subs = new Set()
  return {
    get: () => value,
    set: next => {
      value = typeof next === 'function' ? next(value) : next
      subs.forEach(fn => fn())
    },
    subscribe: fn => {
      subs.add(fn)
      return () => subs.delete(fn)
    },
    reset: () => {
      value = initial
    }
  }
}

function useStore(store) {
  return useSyncExternalStore(store.subscribe, store.get, store.get)
}

const $job = createStore(null) //            { id, kind } while a job runs
const $preview = createStore(null) //        dry-run job result → dialog
const $focusKey = createStore(null) //       candidate key to reveal on the page
const $seenRun = createStore(0) //           last run the user has seen (statusbar chip)
const $dismissed = createStore([]) //        change ids the user kept (hidden)
const STORES = [$job, $preview, $focusKey, $seenRun, $dismissed]

function stored(key, fallback) {
  try {
    return storage && typeof storage.get === 'function' ? storage.get(key, fallback) ?? fallback : fallback
  } catch {
    return fallback
  }
}

function remember(key, value) {
  try {
    if (storage && typeof storage.set === 'function') storage.set(key, value)
  } catch {
    /* storage is best effort */
  }
}

// ==== rpc + data hooks ======================================================

function rpc(method, params = {}, timeoutMs) {
  if (typeof host.request !== 'function') return Promise.reject(new Error('host.request unavailable'))
  return host.request(RPC_PREFIX + method, params, timeoutMs)
}

/** -32601: the gateway does not know the method → Python half not enabled. */
function isDisabledError(err) {
  if (!err) return false
  if (err.code === -32601 || err?.data?.code === -32601) return true
  return /-32601|method not found|unknown method/i.test(String(err.message || err))
}

function errorText(err) {
  const detail = err?.data?.detail
  return String(detail || err?.message || err || '')
}

function useGatewayOpen() {
  const atom = host.state && host.state.gateway
  const state = atom && typeof useValue === 'function' ? useValue(atom) : 'open'
  return state === 'open'
}

function useSomniaQuery(parts, method, params, options = {}) {
  const open = useGatewayOpen()
  return useQuery({
    queryKey: [QK, ...parts],
    queryFn: () => rpc(method, params),
    enabled: open && options.enabled !== false,
    staleTime: options.staleTime ?? 15000,
    refetchInterval: options.refetchInterval,
    retry: (count, err) => !isDisabledError(err) && count < 2
  })
}

const useOverview = () => useSomniaQuery(['overview'], 'overview', {}, { refetchInterval: OVERVIEW_POLL_MS })
const useChanges = run => useSomniaQuery(['changes', run ?? 'latest'], 'changes', { run: run ?? 'latest' })
const useCandidates = (filter, q) =>
  useSomniaQuery(['candidates', filter, q || ''], 'candidates', { filter, q: q || undefined, limit: 100 })
const useCandidate = (key, enabled = true) =>
  useSomniaQuery(['candidate', key], 'candidate', { key }, { enabled: !!key && enabled })
const useThemes = () => useSomniaQuery(['themes'], 'themes', {}, { staleTime: 60000 })
const useDiary = () => useSomniaQuery(['diary'], 'diary', { limit: 12 }, { staleTime: 60000 })
const useStats = () => useSomniaQuery(['stats'], 'stats', {}, { staleTime: 60000 })
const useFed = () => useSomniaQuery(['sessions.fed'], 'sessions.fed', {}, { staleTime: 5 * 60000 })

function useInvalidate() {
  const client = typeof useQueryClient === 'function' ? useQueryClient() : sdk.queryClient
  return useCallback(() => client && client.invalidateQueries({ queryKey: [QK] }), [client])
}

/** Every write goes through here: one call, one toast, one cache refresh. */
function useWrite() {
  const invalidate = useInvalidate()
  const mutation = useMutation({
    mutationFn: ({ method, params }) => rpc(method, params),
    onSuccess: (_data, vars) => {
      invalidate()
      if (vars.success) toast('success', vars.success)
    },
    onError: err => toastError(err)
  })
  const write = useCallback(
    (method, params, success) => mutation.mutateAsync({ method, params, success }),
    [mutation.mutateAsync]
  )
  return { write, pending: mutation.isPending, variables: mutation.variables }
}

/** Start a background job (run / dry run / promote). Polling lives in JobWatcher. */
async function startJob(kind, method, params) {
  if ($job.get()) {
    toast('info', tr('toast.alreadyRunning'))
    return null
  }
  try {
    const res = await rpc(method, params)
    const id = res && (res.job || res.id)
    if (!id) throw new Error('no job id returned')
    $job.set({ id, kind, startedAt: Date.now() })
    return id
  } catch (err) {
    toastError(err)
    return null
  }
}

/** Polls `job` every 2 s while one runs; on finish refreshes everything once. */
function JobWatcher() {
  const job = useStore($job)
  const invalidate = useInvalidate()
  const open = useGatewayOpen()
  const query = useQuery({
    queryKey: [QK, 'job', job ? job.id : null],
    queryFn: () => rpc('job', { id: job.id }),
    enabled: open && !!job,
    refetchInterval: q => (q.state.data && q.state.data.state !== 'running' ? false : JOB_POLL_MS),
    retry: 1
  })
  const data = query.data
  useEffect(() => {
    if (!job || !data || data.id !== job.id || data.state === 'running') return
    $job.set(null)
    invalidate()
    if (data.state === 'done') {
      if (job.kind === 'dry_run') $preview.set(data.result || { summary: data.summary, changes: [] })
      else toast('success', data.summary || tr('toast.jobDone'), tr('toast.jobDone'))
    } else {
      toast('error', data.error || data.summary || tr('toast.jobFailed'), tr('toast.jobFailed'))
    }
  }, [data, job, invalidate])
  useEffect(() => {
    if (job && query.error) {
      $job.set(null)
      toastError(query.error)
    }
  }, [query.error, job])
  return null
}

function useJobProgress() {
  const job = useStore($job)
  const open = useGatewayOpen()
  const query = useQuery({
    queryKey: [QK, 'job', job ? job.id : null],
    queryFn: () => rpc('job', { id: job.id }),
    enabled: open && !!job,
    refetchInterval: JOB_POLL_MS
  })
  const [, tick] = useState(0)
  useEffect(() => {
    if (!job) return undefined
    const id = setInterval(() => tick(n => n + 1), 1000)
    return () => clearInterval(id)
  }, [job])
  if (!job) return null
  return { ...job, summary: query.data && query.data.summary, seconds: Math.round((Date.now() - job.startedAt) / 1000) }
}

// ==== host doors ============================================================

function toast(kind, message, title) {
  try {
    if (typeof host.notify === 'function') host.notify({ kind, message, title: title || PLUGIN_NAME_TITLE() })
  } catch {
    /* toasts are best effort */
  }
}

function toastError(err) {
  if (typeof host.notifyError === 'function') {
    try {
      host.notifyError(err, errorText(err) || tr('error.load'))
      return
    } catch {
      /* fall through */
    }
  }
  toast('error', errorText(err) || tr('error.load'))
}

const PLUGIN_NAME_TITLE = () => tr('name')

function go(path) {
  if (typeof host.navigate === 'function') host.navigate(path)
}

const canOpenSession = () => typeof host.openSession === 'function'

function openChat(id) {
  if (!canOpenSession()) return
  Promise.resolve(host.openSession(id, { intent: 'stack' })).catch(toastError)
}

const canAsk = () => typeof host.newChat === 'function' && !!host.composer && typeof host.composer.setDraft === 'function'

/** New chat with a prefilled (not sent) question. setDraft is fail-closed until the draft mounts: retry. */
async function askHermes(text) {
  if (!canAsk()) return false
  try {
    host.newChat()
  } catch (err) {
    toastError(err)
    return false
  }
  for (let attempt = 0; attempt < 25; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 120))
    try {
      if (await host.composer.setDraft('new', text)) {
        if (typeof host.composer.focus === 'function') host.composer.focus('new')
        return true
      }
    } catch {
      /* not mounted yet */
    }
  }
  toast('warning', tr('toast.askFailed'))
  return false
}

function SettingsLanding() {
  const t = useTr()
  const ov = useOverview().data
  return h('div', { className: 'smn-page', style: { height: 'auto', overflow: 'visible', maxWidth: '42rem' } },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: '1rem', marginBottom: '1rem' } },
      h('p', { className: 'smn-hint', style: { flex: 1, margin: 0 } }, t('settingsPage.intro')),
      h(Btn, { variant: 'default', size: 'sm', icon: 'sparkle', onClick: () => go(ROUTE) }, t('settingsPage.open'))),
    ov ? h(Thinks, { ov }) : h(Skel, { height: 120 }))
}

function openSettings() {
  if (typeof pluginSettingsHref === 'function') go(pluginSettingsHref(PLUGIN_ID))
}

// ==== formatting ============================================================

const nowS = () => Date.now() / 1000
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
const f2 = n => (Number.isFinite(n) ? n.toFixed(2) : '–')
const pct = n => Math.round(clamp(n || 0, 0, 9.99) * 100)

function locale() {
  try {
    return (i18n && i18n.locale) || (typeof navigator !== 'undefined' && navigator.language) || 'en'
  } catch {
    return 'en'
  }
}

function ago(tsSeconds) {
  if (!tsSeconds) return ''
  const ms = tsSeconds * 1000
  if (typeof relativeTime === 'function') {
    try {
      return relativeTime(ms, Date.now())
    } catch {
      /* fall back */
    }
  }
  const diff = (ms - Date.now()) / 1000
  const abs = Math.abs(diff)
  const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: 'auto' })
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute')
  if (abs < DAY_S) return rtf.format(Math.round(diff / 3600), 'hour')
  return rtf.format(Math.round(diff / DAY_S), 'day')
}

function hhmm(date) {
  return date.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' })
}

function shortDate(input) {
  const date = typeof input === 'number' ? new Date(input * 1000) : new Date(`${input}T12:00:00`)
  if (Number.isNaN(date.getTime())) return String(input)
  return date.toLocaleDateString(locale(), { month: 'short', day: 'numeric' })
}

function whenNext(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const hm = hhmm(date)
  const sameDay = date.toDateString() === now.toDateString()
  const tomorrow = new Date(now.getTime() + DAY_S * 1000).toDateString() === date.toDateString()
  if (sameDay) return date.getHours() >= 18 ? tr('time.tonight', hm) : tr('time.today', hm)
  if (tomorrow) return date.getHours() < 6 ? tr('time.tonight', hm) : tr('time.tomorrow', hm)
  return tr('time.on', date.toLocaleDateString(locale(), { weekday: 'short', day: 'numeric' }), hm)
}

/** 'occam: …' / 'pareto tail: …' → { principle, text } */
function splitPrinciple(reason) {
  if (!reason) return { principle: null, text: '' }
  const m = /^\s*(pareto|lindy|inversion|occam|feynman)\b(?:\s+tail)?\s*:?\s*/i.exec(reason)
  if (!m) return { principle: null, text: reason }
  const rest = reason.slice(m[0].length).trim()
  return { principle: m[1].toLowerCase(), text: rest ? rest[0].toUpperCase() + rest.slice(1) : reason }
}

function promotedWhere(to) {
  if (!to) return ''
  if (to === 'builtin:memory') return 'MEMORY.md'
  if (to === 'builtin:user') return 'USER.md'
  if (to.startsWith('vault:')) return to.slice(6)
  if (to.startsWith('staged:')) return '/memory pending'
  return to
}

const changeId = c => `${c.run_at}:${c.key || c.text.slice(0, 48)}`

function findEntry(budgets, change) {
  const budget = (budgets || []).find(b => b.target === change.target)
  if (!budget) return null
  const entry = budget.entries.find(e => (change.key && e.key === change.key) || e.text === change.text)
  return entry ? { budget, entry } : null
}

// ==== primitives ============================================================

/** h(type, props, ...children) → jsx/jsxs with the key passed the way React wants it. */
function h(type, props, ...children) {
  const { key, ...rest } = props || {}
  const kids = children.flat().filter(c => c !== null && c !== undefined && c !== false)
  if (kids.length === 0) return jsx(type, rest, key)
  if (kids.length === 1) return jsx(type, { ...rest, children: kids[0] }, key)
  return jsxs(type, { ...rest, children: kids }, key)
}

function Icon({ name, size = '0.875rem', spinning, className, style }) {
  if (Codicon) return h(Codicon, { name, size, spinning, className, style })
  return h('span', { className: `smn-glyph ${className || ''}`, style: { fontSize: size, ...style }, 'aria-hidden': true }, '•')
}

function Tooltip({ label, children, side }) {
  if (!label) return children
  if (Tip) return h(Tip, { label, side }, children)
  return isValidElement(children) ? cloneElement(children, { title: typeof label === 'string' ? label : undefined }) : children
}

function Btn({ children, icon, variant = 'ghost', size = 'xs', onClick, disabled, loading, tip, danger, className, ...rest }) {
  const content = [icon ? h(Icon, { key: 'i', name: icon, size: size === 'sm' ? '0.875rem' : '0.8125rem' }) : null, children]
  const cls = `${danger ? 'smn-danger ' : ''}${className || ''}`
  const node = Button
    ? h(Button, { variant, size, onClick, disabled, loading, type: 'button', className: cls, ...rest }, ...content)
    : h('button', { type: 'button', className: `smn-btn smn-btn-${variant} ${cls}`, onClick, disabled: disabled || loading, ...rest }, ...content)
  return tip ? h(Tooltip, { label: tip }, node) : node
}

function Skel({ height = 14, width = '100%', style }) {
  const s = { height, width, ...style }
  return Skeleton ? h(Skeleton, { style: s }) : h('div', { className: 'smn-skel', style: s })
}

function Pill({ color, icon, children, tip, strong }) {
  const node = h(
    'span',
    { className: `smn-pill${strong ? ' smn-pill-strong' : ''}`, style: { '--c': color || 'var(--ui-text-tertiary)' } },
    icon ? h(Icon, { name: icon, size: '0.6875rem' }) : null,
    children
  )
  return tip ? h(Tooltip, { label: tip }, node) : node
}

function Section({ id, title, hint, actions, children, className }) {
  return h(
    'section',
    { id, className: `smn-section ${className || ''}`, 'aria-label': typeof title === 'string' ? title : undefined },
    h(
      'header',
      { className: 'smn-section-head' },
      h('div', { className: 'smn-section-titles' }, h('h2', { className: 'smn-h2' }, title), hint ? h('p', { className: 'smn-hint' }, hint) : null),
      actions ? h('div', { className: 'smn-section-actions' }, actions) : null
    ),
    children
  )
}

function Empty({ icon = 'sparkle', title, children, action }) {
  return h(
    'div',
    { className: 'smn-empty' },
    h(Icon, { name: icon, size: '1.125rem', className: 'smn-empty-icon' }),
    title ? h('p', { className: 'smn-empty-title' }, title) : null,
    children ? h('div', { className: 'smn-empty-body' }, children) : null,
    action ? h('div', { className: 'smn-empty-action' }, action) : null
  )
}

function SessionChips({ sessions, max = 4 }) {
  const t = useTr()
  if (!sessions || !sessions.length) return null
  const shown = sessions.slice(0, max)
  const more = sessions.length - shown.length
  const clickable = canOpenSession()
  return h(
    'div',
    { className: 'smn-chips' },
    shown.map(s =>
      h(
        Tooltip,
        { key: s.id, label: `${s.title || s.id}${s.source ? ` · ${s.source}` : ''} · ${shortDate(s.day)}` },
        h(
          'button',
          {
            type: 'button',
            className: 'smn-chip',
            disabled: !clickable,
            onClick: e => {
              e.stopPropagation()
              openChat(s.id)
            }
          },
          h(Icon, { name: s.source === 'telegram' ? 'comment' : 'comment-discussion', size: '0.6875rem' }),
          h('span', { className: 'smn-chip-text' }, s.title || s.id)
        )
      )
    ),
    more > 0 ? h('span', { className: 'smn-meta' }, `+${more}`) : null,
    null && t
  )
}

function PrincipleChip({ principle }) {
  const t = useTr()
  if (!principle) return null
  const name = principle[0].toUpperCase() + principle.slice(1)
  return h(Pill, { color: 'var(--ui-purple)', icon: PRINCIPLE_ICON[principle] || 'law', tip: principleText(t, principle, 'description') }, name)
}

/** Principle label/description: translation table first, then the server's English, then nothing. */
let serverPrinciples = null
function principleText(t, id, field) {
  const key = `principle.${id}.${field}`
  const local = t(key)
  if (local && local !== key) return local
  const fromServer = serverPrinciples && serverPrinciples[field === 'label' ? 'labels' : 'descriptions']
  return (fromServer && fromServer[id]) || ''
}

// Two-step inline confirm: first click arms, second click fires. Used where a modal would be heavy (chat card).
function ArmedButton({ label, armedLabel, onConfirm, icon, disabled }) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return undefined
    const id = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(id)
  }, [armed])
  return h(Btn, {
    icon,
    disabled,
    danger: armed,
    variant: armed ? 'secondary' : 'ghost',
    onClick: () => (armed ? (setArmed(false), onConfirm()) : setArmed(true))
  }, armed ? armedLabel : label)
}

// ==== score visuals =========================================================

function ScoreRing({ score, min, size = 38 }) {
  const r = 15
  const c = 2 * Math.PI * r
  const value = clamp(score || 0, 0, 1)
  const passes = value >= (min ?? 0.65)
  const minAngle = (min ?? 0.65) * 360 - 90
  const mx = 18 + Math.cos((minAngle * Math.PI) / 180) * r
  const my = 18 + Math.sin((minAngle * Math.PI) / 180) * r
  return h(
    'svg',
    { className: 'smn-ring', width: size, height: size, viewBox: '0 0 36 36', role: 'img', 'aria-label': `score ${f2(value)}` },
    h('circle', { cx: 18, cy: 18, r, fill: 'none', stroke: 'var(--ui-stroke-tertiary)', strokeWidth: 3 }),
    h('circle', {
      cx: 18,
      cy: 18,
      r,
      fill: 'none',
      stroke: passes ? 'var(--ui-accent)' : 'var(--ui-text-tertiary)',
      strokeWidth: 3,
      strokeLinecap: 'round',
      strokeDasharray: `${value * c} ${c}`,
      transform: 'rotate(-90 18 18)'
    }),
    h('circle', { cx: mx, cy: my, r: 1.6, fill: 'var(--ui-text-primary)', opacity: 0.55 }),
    h('text', { x: 18, y: 21.5, textAnchor: 'middle', className: 'smn-ring-text' }, f2(value).replace(/^0/, ''))
  )
}

/**
 * The six signals as bars whose full length is each signal's weight, so the
 * filled part is literally its contribution to the score. Bonuses follow as chips.
 */
function SignalBars({ components, score, min, compact }) {
  const t = useTr()
  if (!components) return null
  const maxW = SIGNALS[0][1]
  const bonuses = BONUSES.filter(b => (components[b] || 0) > 0)
  return h(
    'div',
    { className: `smn-signals${compact ? ' smn-signals-compact' : ''}` },
    SIGNALS.map(([name, weight]) => {
      const value = clamp(components[name] || 0, 0, 1)
      const contrib = weight * value
      return h(
        Tooltip,
        { key: name, label: `${t(`signals.help.${name}`)} ${t('signals.contribution', weight, f2(value), f2(contrib))}` },
        h(
          'div',
          { className: 'smn-signal', tabIndex: 0 },
          h('span', { className: 'smn-signal-name' }, t(`signals.names.${name}`)),
          h(
            'span',
            { className: 'smn-signal-track' },
            h('span', { className: 'smn-signal-cap', style: { width: `${(weight / maxW) * 100}%` } },
              h('span', { className: 'smn-signal-fill', style: { width: `${value * 100}%` } }))
          ),
          h('span', { className: 'smn-signal-val' }, `+${f2(contrib).replace(/^0/, '')}`)
        )
      )
    }),
    bonuses.length || score != null
      ? h(
          'div',
          { className: 'smn-signal-foot' },
          bonuses.map(b =>
            h(
              Tooltip,
              { key: b, label: t(`signals.help.${b}`) },
              h(
                'span',
                { className: `smn-bonus${b === 'phase_boost' ? '' : ' smn-bonus-principle'}`, tabIndex: 0 },
                b === 'phase_boost' ? null : h(Icon, { name: PRINCIPLE_ICON[b], size: '0.6875rem' }),
                `${t(`signals.names.${b}`)} +${f2(components[b]).replace(/^0/, '')}`
              )
            )
          ),
          score != null
            ? h('span', { className: 'smn-signal-total' }, `${t('signals.total')} ${f2(score)}`, min != null ? h('span', { className: 'smn-meta' }, ` · ${t('signals.min', f2(min))}`) : null)
            : null
        )
      : null
  )
}

function ScoreBar({ score, min }) {
  const value = clamp(score || 0, 0, 1)
  const passes = value >= (min ?? 0.65)
  return h(
    'div',
    { className: 'smn-scorebar', role: 'img', 'aria-label': `score ${f2(value)} of minimum ${f2(min)}` },
    h('div', { className: 'smn-scorebar-track' },
      h('div', { className: `smn-scorebar-fill${passes ? ' is-pass' : ''}`, style: { width: `${value * 100}%` } }),
      h('div', { className: 'smn-scorebar-min', style: { left: `${(min ?? 0.65) * 100}%` } })),
    h('span', { className: 'smn-scorebar-val' }, f2(value))
  )
}

function Diff({ removed, added }) {
  if ((!removed || !removed.length) && !added) return null
  return h(
    'div',
    { className: 'smn-diff' },
    (removed || []).map((line, i) => h('div', { key: `r${i}`, className: 'smn-diff-line is-del' }, h('span', { className: 'smn-diff-sign' }, '−'), h('span', null, line))),
    added ? h('div', { className: 'smn-diff-line is-add' }, h('span', { className: 'smn-diff-sign' }, '+'), h('span', null, added)) : null
  )
}

// ==== confirm host ==========================================================

/** One confirm dialog per page; `ask({...})` opens it. */
function useConfirm() {
  const [state, setState] = useState(null)
  const t = useTr()
  const ask = useCallback(opts => setState(opts), [])
  const node = state
    ? ConfirmDialog
      ? h(ConfirmDialog, {
          open: true,
          onClose: () => setState(null),
          onConfirm: state.onConfirm,
          title: state.title,
          description: state.description,
          confirmLabel: state.confirmLabel,
          cancelLabel: t('common.cancel'),
          destructive: !!state.destructive
        })
      : h(FallbackConfirm, { ...state, onClose: () => setState(null) })
    : null
  return [node, ask]
}

function FallbackConfirm({ title, description, confirmLabel, onConfirm, onClose }) {
  const t = useTr()
  return h(
    'div',
    { className: 'smn-modal-backdrop', role: 'dialog', 'aria-modal': true },
    h(
      'div',
      { className: 'smn-modal' },
      h('h3', { className: 'smn-h2' }, title),
      description ? h('p', { className: 'smn-body' }, description) : null,
      h('div', { className: 'smn-row-actions smn-end' },
        h(Btn, { onClick: onClose }, t('common.cancel')),
        h(Btn, { variant: 'default', size: 'sm', onClick: async () => { await onConfirm(); onClose() } }, confirmLabel))
    )
  )
}

// ==== header ================================================================

/** A faint, deterministic star field. Pure decoration, drawn in theme ink. */
function StarField() {
  const stars = useMemo(() => {
    let s = 11
    const rnd = () => ((s = (s * 16807) % 2147483647) - 1) / 2147483646
    return Array.from({ length: 46 }, () => ({ x: rnd() * 100, y: rnd() * 100, r: 0.35 + rnd() * 0.9, o: 0.25 + rnd() * 0.6 }))
  }, [])
  return h(
    'svg',
    { className: 'smn-stars', 'aria-hidden': true, preserveAspectRatio: 'none', viewBox: '0 0 100 100' },
    stars.map((st, i) => h('ellipse', { key: i, cx: st.x, cy: st.y, rx: st.r * 0.16, ry: st.r * 0.55, fill: 'var(--ui-text-primary)', opacity: st.o * 0.5 }))
  )
}

function Moon() {
  const id = useId().replace(/:/g, '')
  return h(
    'svg',
    { className: 'smn-moon', viewBox: '0 0 32 32', width: 30, height: 30, 'aria-hidden': true },
    h('defs', null, h('mask', { id: `m${id}` }, h('rect', { width: 32, height: 32, fill: 'white' }), h('circle', { cx: 21, cy: 11, r: 10.5, fill: 'black' }))),
    h('circle', { cx: 16, cy: 16, r: 12, fill: 'var(--ui-accent)', mask: `url(#m${id})` }),
    h('circle', { cx: 25.5, cy: 23.5, r: 0.9, fill: 'var(--ui-accent)', opacity: 0.7 }),
    h('circle', { cx: 28.5, cy: 17.5, r: 0.55, fill: 'var(--ui-accent)', opacity: 0.5 })
  )
}

function StateLine({ ov }) {
  const t = useTr()
  const parts = []
  parts.push(ov.last_run ? t('header.lastDream', ago(ov.last_run.at)) : t('header.neverDreamed'))
  if (ov.cron && ov.cron.installed && ov.cron.next_run_at) parts.push(t('header.next', whenNext(ov.cron.next_run_at)))
  else parts.push(t('header.noSchedule'))
  parts.push(t('header.writesTo', t(`header.dest.${ov.destinations}`) || ov.destinations))
  if (ov.destructive === 'stage') parts.push(t('header.stage'))
  return h(
    'p',
    { className: 'smn-state' },
    h('span', { className: `smn-dot${ov.alarm ? ' is-warn' : ov.last_run ? ' is-ok' : ''}`, 'aria-hidden': true }),
    parts.join(' · '),
    ov.llm && ov.llm.model ? h(Tooltip, { label: t('header.model', `${ov.llm.provider ? `${ov.llm.provider}/` : ''}${ov.llm.model}`) }, h('span', { className: 'smn-state-model', tabIndex: 0 }, h(Icon, { name: 'info', size: '0.75rem' }))) : null
  )
}

function Header({ ov, canUndo, onUndo }) {
  const t = useTr()
  const progress = useJobProgress()
  const busy = !!progress
  const label = progress ? t(progress.kind === 'dry_run' ? 'header.jobDry' : progress.kind === 'promote' ? 'header.jobPromote' : 'header.jobRun') : ''
  return h(
    'header',
    { className: 'smn-header' },
    h(StarField),
    h(
      'div',
      { className: 'smn-header-main' },
      h(Moon),
      h(
        'div',
        { className: 'smn-header-text' },
        h('h1', { className: 'smn-h1' }, t('name')),
        ov ? h(StateLine, { ov }) : h(Skel, { height: 12, width: 280 }),
        busy
          ? h('p', { className: 'smn-progress', role: 'status' },
              h(Icon, { name: 'loading', spinning: true, size: '0.75rem' }),
              t('header.running', label, progress.seconds),
              progress.summary ? h('span', { className: 'smn-meta' }, ` · ${progress.summary}`) : null)
          : null
      )
    ),
    h(
      'div',
      { className: 'smn-header-actions' },
      h(Btn, { size: 'sm', icon: 'eye', disabled: busy || !ov, tip: t('header.previewHint'), onClick: () => startJob('dry_run', 'run', { dry_run: true }) }, t('header.preview')),
      h(Btn, { size: 'sm', icon: 'discard', disabled: busy || !canUndo, tip: t('header.undoHint'), onClick: onUndo }, t('header.undo')),
      typeof pluginSettingsHref === 'function'
        ? h(Btn, { size: 'sm', icon: 'gear', tip: t('header.settings'), 'aria-label': t('header.settings'), onClick: openSettings })
        : null,
      h(Btn, { size: 'sm', variant: 'default', icon: 'sparkle', loading: busy && progress.kind === 'run', disabled: busy || !ov, onClick: () => startJob('run', 'run', {}) }, busy && progress.kind === 'run' ? t('header.dreaming') : t('header.dreamNow'))
    )
  )
}

// ==== alarm =================================================================

function AlarmBanner({ alarm, onReview }) {
  const t = useTr()
  if (!alarm) return null
  const titleKey = `alarm.title.${alarm.kind}`
  const title = typeof lookup(EN, titleKey) === 'function' ? t(titleKey, alarm.nights || 1) : t(titleKey)
  const act = alarm.action
  const onAction = () => {
    if (act === 'run') startJob('run', 'run', {})
    else if (act === 'settings') openSettings()
    else onReview(alarm)
  }
  const tone = alarm.kind === 'errors' || alarm.kind === 'stale_cron' ? 'var(--ui-red)' : 'var(--ui-yellow)'
  return h(
    'div',
    { className: 'smn-alarm', role: 'alert', style: { '--c': tone } },
    h(Icon, { name: alarm.kind === 'budget_full' ? 'layers' : 'warning', size: '1rem', className: 'smn-alarm-icon' }),
    h(
      'div',
      { className: 'smn-alarm-text' },
      h('p', { className: 'smn-alarm-title' }, title),
      h('p', { className: 'smn-body' }, alarm.message),
      alarm.best
        ? h('div', { className: 'smn-alarm-best' },
            h('span', { className: 'smn-meta' }, t('alarm.closest')),
            h('span', { className: 'smn-alarm-best-text' }, alarm.best.text),
            h(ScoreBar, { score: alarm.best.score, min: alarm.best.min_score }),
            alarm.best.missing && alarm.best.missing[0] ? h('span', { className: 'smn-missing' }, alarm.best.missing[0]) : null)
        : null
    ),
    act ? h(Btn, { size: 'sm', variant: 'secondary', onClick: onAction }, t(`alarm.action.${act}`)) : null
  )
}

// ==== this morning ==========================================================

function MorningSection({ ov, run, onBack, confirm }) {
  const t = useTr()
  const q = useChanges(run)
  const dismissed = useStore($dismissed)
  const [showKept, setShowKept] = useState(false)
  const data = q.data
  const changes = (data && data.changes) || []
  const visible = showKept ? changes : changes.filter(c => !dismissed.includes(changeId(c)))
  const keptCount = changes.length - changes.filter(c => !dismissed.includes(changeId(c))).length
  const isLatest = run == null
  const recent = data && data.run_at && nowS() - data.run_at < 1.25 * DAY_S
  const title = !isLatest
    ? t('morning.titleNight', shortDate(data && data.run_at ? data.run_at : run))
    : recent || !data || !data.run_at
      ? t('morning.title')
      : t('morning.titleOlder', shortDate(data.run_at))
  const actions = [
    keptCount > 0 && !showKept ? h(Btn, { key: 'k', onClick: () => setShowKept(true) }, `${t('morning.kept', keptCount)} · ${t('morning.showKept')}`) : null,
    !isLatest ? h(Btn, { key: 'b', icon: 'reply', onClick: onBack }, t('morning.back')) : null
  ]
  let body
  if (q.isLoading || !ov) body = h('div', { className: 'smn-list' }, [0, 1].map(i => h('div', { key: i, className: 'smn-change' }, h(Skel, { height: 38, width: 38, style: { borderRadius: 19 } }), h('div', { className: 'smn-grow' }, h(Skel, { height: 12, width: '40%' }), h(Skel, { height: 14, style: { marginTop: 8 } }), h(Skel, { height: 14, width: '70%', style: { marginTop: 6 } })))))
  else if (q.error) body = h(Empty, { icon: 'error', title: tr('error.load') }, errorText(q.error))
  else if (!ov.last_run && isLatest) {
    const when = ov.cron && ov.cron.installed && ov.cron.next_run_at ? whenNext(ov.cron.next_run_at) : null
    body = h(
      'div',
      { className: 'smn-first' },
      h(Moon),
      h('div', null,
        h('p', { className: 'smn-empty-title' }, t('morning.neverTitle')),
        h('p', { className: 'smn-body' }, when ? t('morning.neverBody', when) : t('morning.neverNoCron')),
        h('div', { className: 'smn-row-actions' },
          h(Btn, { size: 'sm', variant: 'default', icon: 'sparkle', onClick: () => startJob('run', 'run', {}) }, t('header.dreamNow')),
          h(Btn, { size: 'sm', icon: 'eye', onClick: () => startJob('dry_run', 'run', { dry_run: true }) }, t('header.preview'))))
    )
  } else if (!changes.length) body = h(Empty, { icon: 'eye-closed', title: t('morning.nothing') }, t('morning.nothingHint'))
  else if (!visible.length) body = null
  else body = h('div', { className: 'smn-list' }, visible.map(c => h(ChangeCard, { key: changeId(c), change: c, ov, confirm, kept: dismissed.includes(changeId(c)) })))
  return h(Section, { id: 'smn-morning', title, hint: t('morning.hint'), actions }, body)
}

function ChangeCard({ change, ov, confirm, kept }) {
  const t = useTr()
  const { write, pending } = useWrite()
  const [editing, setEditing] = useState(false)
  const located = findEntry(ov && ov.budgets, change)
  const reason = splitPrinciple(change.reason)
  const isWrite = ['added', 'merged', 'superseded', 'vault', 'staged'].includes(change.action)
  const evicted = change.action === 'evicted'
  const id = changeId(change)
  const keep = () => {
    const next = [...$dismissed.get().filter(x => x !== id), id].slice(-300)
    $dismissed.set(next)
    remember('dismissed', next)
  }
  const forget = () =>
    confirm({
      title: t('confirm.forgetTitle'),
      description: t('confirm.forgetBody'),
      confirmLabel: t('confirm.forgetLabel'),
      destructive: true,
      onConfirm: () => write('forget', { key: change.key, remove_entry: true }, t('toast.forgotten'))
    })
  const actions = []
  if (!kept) actions.push(h(Btn, { key: 'keep', icon: 'check', tip: t('morning.keepHint'), onClick: keep }, t('morning.keep')))
  if (change.key && (isWrite || change.action === 'deferred' || change.action === 'skipped'))
    actions.push(h(Btn, { key: 'forget', icon: 'trash', onClick: forget }, t('morning.forget')))
  if (located && !editing && isWrite) actions.push(h(Btn, { key: 'edit', icon: 'edit', onClick: () => setEditing(true) }, t('morning.edit')))
  if (located && ov.vault && change.target !== 'vault' && isWrite)
    actions.push(h(Btn, { key: 'vault', icon: 'file-symlink-file', disabled: pending, onClick: () => write('entry.to_vault', { target: located.budget.target, index: located.entry.index, old: located.entry.text }, t('toast.moved')) }, t('morning.toVault')))
  if (change.key && (change.action === 'deferred' || change.action === 'skipped')) {
    actions.push(h(Btn, { key: 'promote', icon: 'arrow-up', onClick: () => startJob('promote', 'promote', { key: change.key }) }, t('morning.promote')))
    actions.push(h(Btn, { key: 'pin', icon: 'pin', tip: t('morning.pinHint'), onClick: () => write('pin', { key: change.key, pinned: true }, t('toast.pinned')) }, t('morning.pin')))
  }
  if (canAsk()) actions.push(h(Btn, { key: 'ask', icon: 'comment-discussion', onClick: () => askHermes(t('ask.memory', change.text)) }, t('morning.ask')))

  return h(
    'article',
    { className: `smn-change${kept ? ' is-kept' : ''}`, 'data-action': change.action },
    h('div', { className: 'smn-change-ring' }, evicted ? h('div', { className: 'smn-evicted-mark' }, h(Icon, { name: 'remove', size: '1rem' })) : h(ScoreRing, { score: change.score, min: ov && ov.settings ? ov.settings.min_score : 0.65 })),
    h(
      'div',
      { className: 'smn-change-body' },
      h(
        'div',
        { className: 'smn-change-meta' },
        h(Pill, { color: ACTION_COLOR[change.action], icon: ACTION_ICON[change.action], strong: true }, t(`morning.action.${change.action}`)),
        h('span', { className: 'smn-meta smn-mono' }, t(`morning.target.${change.target}`) || change.target),
        reason.principle ? h(PrincipleChip, { principle: reason.principle }) : null,
        change.replaced && change.replaced.length ? h('span', { className: 'smn-meta' }, t('morning.replaced', change.replaced.length)) : null
      ),
      editing && located
        ? h(EntryEditor, { budget: located.budget, entry: located.entry, onDone: () => setEditing(false) })
        : evicted
          ? h(Diff, { removed: [change.text] })
          : change.replaced && change.replaced.length
            ? h(Diff, { removed: change.replaced, added: change.text })
            : h('p', { className: 'smn-fact' }, change.text),
      reason.text ? h('p', { className: 'smn-reason' }, reason.text) : null,
      evicted ? h('p', { className: 'smn-reason' }, t('morning.evictedNote')) : null,
      change.action === 'staged' ? h('p', { className: 'smn-reason' }, t('morning.stagedNote')) : null,
      change.sessions && change.sessions.length ? h('div', { className: 'smn-change-from' }, h('span', { className: 'smn-meta' }, t('morning.from')), h(SessionChips, { sessions: change.sessions })) : null,
      actions.length ? h('div', { className: 'smn-row-actions' }, actions) : null
    ),
    !evicted ? h('div', { className: 'smn-change-why' }, h(SignalBars, { components: change.components, compact: true })) : null
  )
}

function EntryEditor({ budget, entry, onDone }) {
  const t = useTr()
  const { write, pending } = useWrite()
  const [text, setText] = useState(entry.text)
  const delta = text.length - entry.text.length
  const free = budget.limit - budget.used - delta
  const ref = useRef(null)
  useEffect(() => {
    if (ref.current) ref.current.focus()
  }, [])
  const save = async () => {
    try {
      await write('entry.edit', { target: budget.target, index: entry.index, old: entry.text, text: text.trim() }, t('toast.edited'))
      onDone()
    } catch {
      /* toast shown by useWrite */
    }
  }
  const props = {
    ref,
    value: text,
    rows: Math.min(6, Math.max(2, Math.ceil(text.length / 70))),
    className: 'smn-textarea',
    onChange: e => setText(e.target.value),
    onKeyDown: e => {
      if (e.key === 'Escape') onDone()
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save()
    }
  }
  return h(
    'div',
    { className: 'smn-editor' },
    Textarea ? h(Textarea, props) : h('textarea', props),
    h(
      'div',
      { className: 'smn-editor-foot' },
      h('span', { className: `smn-meta${free < 0 ? ' smn-bad' : ''}` }, free < 0 ? t('space.tooLong', -free) : t('space.left', free)),
      h('div', { className: 'smn-row-actions' },
        h(Btn, { onClick: onDone }, t('common.cancel')),
        h(Btn, { variant: 'default', loading: pending, disabled: free < 0 || !text.trim() || text === entry.text, onClick: save }, t('common.save')))
    )
  )
}

// ==== memory space ==========================================================

function MemorySpace({ ov, latestKeys, confirm, focusRef }) {
  const t = useTr()
  const [sel, setSel] = useState(null) // { target, index }
  if (!ov) return h(Section, { title: t('space.title') }, h(Skel, { height: 22 }), h(Skel, { height: 22, style: { marginTop: 18 } }))
  const budgets = ov.budgets || []
  const selected = sel ? budgets.find(b => b.target === sel.target) : null
  const entry = selected ? selected.entries.find(e => e.index === sel.index && (!sel.text || e.text === sel.text)) : null
  const staleCount = budgets.reduce((n, b) => n + b.entries.filter(e => e.stale).length, 0)
  const firstStale = () => {
    for (const b of budgets) {
      const e = b.entries.find(x => x.stale)
      if (e) return setSel({ target: b.target, index: e.index, text: e.text })
    }
  }
  return h(
    Section,
    { id: 'smn-space', title: t('space.title'), hint: t('space.hint') },
    h(
      'div',
      { className: 'smn-space', ref: focusRef },
      budgets.map(b => h(CapacityBar, { key: b.target, budget: b, latestKeys, selected: sel && sel.target === b.target ? sel.index : null, onSelect: e => setSel(e ? { target: b.target, index: e.index, text: e.text } : null) })),
      h(Legend, { budgets }),
      budgets.filter(b => b.used / b.limit >= 0.9).map(b =>
        h('div', { key: `p${b.target}`, className: 'smn-pressure', role: 'status' },
          h(Icon, { name: 'warning', size: '0.8125rem' }),
          h('span', null, t('space.pressure', b.target === 'user' ? 'USER.md' : 'MEMORY.md', pct(b.used / b.limit))),
          staleCount ? h(Btn, { onClick: firstStale }, t('space.reviewStale', staleCount)) : null)
      ),
      entry
        ? h(EntryDetail, { key: `${selected.target}:${entry.index}`, budget: selected, entry, vault: !!ov.vault, confirm, onClose: () => setSel(null) })
        : h('p', { className: 'smn-meta smn-space-pick' }, t('space.pick'))
    )
  )
}

function CapacityBar({ budget, selected, onSelect, latestKeys }) {
  const t = useTr()
  const file = budget.target === 'user' ? 'USER.md' : 'MEMORY.md'
  const ratio = budget.used / budget.limit
  const goalPct = clamp((budget.goal / budget.limit) * 100, 0, 100)
  return h(
    'div',
    { className: 'smn-cap' },
    h(
      'div',
      { className: 'smn-cap-head' },
      h('span', { className: 'smn-cap-file smn-mono' }, file),
      h('span', { className: `smn-cap-num${ratio >= 0.9 ? ' smn-warn' : ''}` }, t('space.used', budget.used, budget.limit), h('span', { className: 'smn-meta' }, ` · ${pct(ratio)}%`))
    ),
    h(
      'div',
      { className: 'smn-cap-bar', role: 'list', 'aria-label': file },
      budget.entries.map(e => {
        const w = (e.chars / budget.limit) * 100
        const cls = ['smn-seg', `is-${e.owner}`, e.stale ? 'is-stale' : '', e.pointer ? 'is-pointer' : '', selected === e.index ? 'is-selected' : '', e.key && latestKeys.has(e.key) ? 'is-new' : ''].join(' ')
        return h(
          Tooltip,
          { key: `${e.index}:${e.chars}`, label: `${e.text.length > 160 ? `${e.text.slice(0, 157)}…` : e.text}  (${e.chars})` },
          h('button', { type: 'button', role: 'listitem', className: cls, style: { width: `${w}%` }, 'aria-pressed': selected === e.index, 'aria-label': e.text.slice(0, 80), onClick: () => onSelect(selected === e.index ? null : e) })
        )
      }),
      h('span', { className: 'smn-cap-goal', style: { left: `${goalPct}%` }, 'aria-hidden': true })
    ),
    h('div', { className: 'smn-cap-scale', 'aria-hidden': true }, h('span', { style: { left: `${goalPct}%` } }, t('space.goal')))
  )
}

function Legend({ budgets }) {
  const t = useTr()
  const all = budgets.flatMap(b => b.entries)
  const n = f => all.filter(f).length
  const items = [
    ['somnia', n(e => e.owner === 'somnia' && !e.stale && !e.pointer)],
    ['human', n(e => e.owner === 'human')],
    ['stale', n(e => e.stale)],
    ['pointer', n(e => e.pointer)]
  ].filter(([, c]) => c > 0)
  return h('div', { className: 'smn-legend' }, items.map(([k, c]) => h('span', { key: k, className: 'smn-legend-item' }, h('span', { className: `smn-swatch is-${k}` }), `${t(`space.legend.${k}`)} ${c}`)))
}

function EntryDetail({ budget, entry, vault, confirm, onClose }) {
  const t = useTr()
  const { write, pending } = useWrite()
  const [editing, setEditing] = useState(false)
  const base = { target: budget.target, index: entry.index, old: entry.text }
  const remove = reject =>
    confirm({
      title: reject ? t('confirm.forgetTitle') : t('confirm.removeTitle'),
      description: reject ? t('confirm.forgetBody') : t('confirm.removeBody'),
      confirmLabel: reject ? t('confirm.forgetLabel') : t('confirm.removeLabel'),
      destructive: true,
      onConfirm: async () => {
        await write('entry.remove', { ...base, reject }, reject ? t('toast.forgotten') : t('toast.removed'))
        onClose()
      }
    })
  const rows = [
    [t('space.owner'), entry.owner === 'somnia' ? t('space.ownerSomnia') : t('space.ownerHuman')],
    [t('space.created'), entry.created_at ? `${shortDate(entry.created_at)} · ${ago(entry.created_at)}` : '—'],
    entry.owner === 'somnia' ? [t('space.confirmed'), entry.last_confirmed_at ? ago(entry.last_confirmed_at) : t('space.never')] : null,
    [t('space.size'), t('common.chars', entry.chars)]
  ].filter(Boolean)
  return h(
    'div',
    { className: 'smn-detail', role: 'region', 'aria-label': t('space.title') },
    h('div', { className: 'smn-detail-head' },
      h('span', { className: `smn-swatch is-${entry.pointer ? 'pointer' : entry.stale ? 'stale' : entry.owner}` }),
      h('span', { className: 'smn-meta smn-mono' }, `${budget.target === 'user' ? 'USER.md' : 'MEMORY.md'} #${entry.index + 1}`),
      h('span', { className: 'smn-grow' }),
      h(Btn, { icon: 'close', 'aria-label': t('common.close'), onClick: onClose })),
    editing ? h(EntryEditor, { budget, entry, onDone: () => setEditing(false) }) : h('p', { className: 'smn-fact smn-selectable' }, entry.text),
    entry.stale ? h('p', { className: 'smn-note is-warn' }, h(Icon, { name: 'history', size: '0.75rem' }), t('space.stale')) : null,
    entry.pointer ? h('p', { className: 'smn-note' }, h(Icon, { name: 'link', size: '0.75rem' }), t('space.pointer')) : null,
    h('dl', { className: 'smn-meta-grid' }, rows.map(([k, v]) => h(Fragment, { key: k }, h('dt', null, k), h('dd', null, v)))),
    !editing
      ? h('div', { className: 'smn-row-actions' },
          h(Btn, { icon: 'edit', onClick: () => setEditing(true) }, t('space.edit')),
          h(Btn, { icon: 'trash', tip: t('space.removeHint'), onClick: () => remove(false) }, t('space.remove')),
          entry.key ? h(Btn, { icon: 'circle-slash', tip: t('space.forgetHint'), onClick: () => remove(true) }, t('space.forget')) : null,
          vault && !entry.pointer ? h(Btn, { icon: 'file-symlink-file', disabled: pending, onClick: () => write('entry.to_vault', base, t('toast.moved')).then(onClose, () => {}) }, t('space.toVault')) : null)
      : null
  )
}

// ==== almost there ==========================================================

const TABS = [
  ['near', 'near', 'near'],
  ['promoted', 'promoted', 'promoted'],
  ['rejected', 'rejected', 'rejected'],
  ['skills', 'skills', 'skills']
]

function AlmostThere({ ov, theme, onClearTheme, themes, confirm, sectionRef }) {
  const t = useTr()
  const [tab, setTab] = useState('near')
  const [q, setQ] = useState('')
  const [debounced, setDebounced] = useState('')
  const [expanded, setExpanded] = useState(null)
  const [limit, setLimit] = useState(8)
  const focusKey = useStore($focusKey)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(q.trim()), 250)
    return () => clearTimeout(id)
  }, [q])
  const query = useCandidates(tab, debounced)
  const items = useMemo(() => {
    const list = (query.data && query.data.items) || []
    if (!theme) return list
    const keys = new Set(((themes || []).find(x => x.tag === theme) || {}).keys || [])
    return list.filter(c => keys.has(c.key) || (c.tags || []).includes(theme))
  }, [query.data, theme, themes])
  // A chat card asked to reveal a candidate: open the right tab and expand it.
  useEffect(() => {
    if (!focusKey) return
    const found = items.find(c => c.key === focusKey)
    if (found) {
      setExpanded(found.key)
      $focusKey.set(null)
      requestAnimationFrame(() => {
        const el = document.querySelector(`[data-key="${CSS.escape(found.key)}"]`)
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      })
    }
  }, [focusKey, items])
  const counts = (ov && ov.counts) || {}
  const countFor = { near: counts.near, promoted: counts.promoted, rejected: counts.rejected, skills: counts.skills }
  const options = TABS.map(([id]) => ({ id, label: countFor[id] != null ? `${t(`near.tabs.${id}`)} ${countFor[id]}` : t(`near.tabs.${id}`) }))
  const switcher = SegmentedControl
    ? h(SegmentedControl, { options, value: tab, sizing: 'content', onChange: id => { setTab(id); setExpanded(null); setLimit(8) } })
    : h('div', { className: 'smn-tabs' }, options.map(o => h('button', { key: o.id, type: 'button', 'aria-pressed': tab === o.id, className: 'smn-tab', onClick: () => setTab(o.id) }, o.label)))
  const search = SearchField
    ? h(SearchField, { placeholder: t('near.search'), value: q, onChange: setQ, onClear: () => setQ(''), containerClassName: 'smn-search', loading: query.isFetching && !!debounced })
    : h('input', { className: 'smn-input', placeholder: t('near.search'), value: q, onChange: e => setQ(e.target.value) })
  let body
  if (query.isLoading) body = h('div', { className: 'smn-list' }, [0, 1, 2].map(i => h('div', { key: i, className: 'smn-cand' }, h(Skel, { height: 14, width: '80%' }), h(Skel, { height: 8, width: '40%', style: { marginTop: 10 } }))))
  else if (query.error) body = h(Empty, { icon: 'error', title: tr('error.load') }, errorText(query.error))
  else if (!items.length) body = h(Empty, { icon: tab === 'skills' ? 'tools' : tab === 'rejected' ? 'circle-slash' : 'telescope' }, debounced || theme ? t('near.noMatch') : t(`near.empty.${tab}`))
  else
    body = h(
      Fragment,
      null,
      h('div', { className: 'smn-list' }, items.slice(0, limit).map(c => h(CandidateRow, { key: c.key, c, tab, open: expanded === c.key, onToggle: () => setExpanded(expanded === c.key ? null : c.key), confirm, vault: !!(ov && ov.vault) }))),
      items.length > limit ? h('div', { className: 'smn-more' }, h(Btn, { icon: 'chevron-down', onClick: () => setLimit(limit + 12) }, t('common.showMore', Math.min(12, items.length - limit)))) : null
    )
  return h(
    Section,
    { id: 'smn-near', title: t('near.title'), hint: t('near.hint') },
    h('div', { className: 'smn-near-tools', ref: sectionRef }, switcher, h('div', { className: 'smn-grow' }), search),
    theme ? h('div', { className: 'smn-filter' }, h(Pill, { color: 'var(--ui-accent)', icon: 'symbol-namespace', strong: true }, t('near.theme', theme)), h(Btn, { icon: 'close', 'aria-label': t('near.clearTheme'), onClick: onClearTheme })) : null,
    body
  )
}

function CandidateRow({ c, tab, open, onToggle, confirm, vault }) {
  const t = useTr()
  const { write } = useWrite()
  const job = useStore($job)
  const deferred = splitPrinciple(c.deferred_reason)
  const allPass = c.status === 'candidate' && (!c.missing || !c.missing.length)
  const forget = () =>
    confirm({
      title: t('confirm.forgetTitle'),
      description: t('confirm.forgetBody'),
      confirmLabel: t('confirm.forgetLabel'),
      destructive: true,
      onConfirm: () => write('forget', { key: c.key, remove_entry: c.status === 'promoted' }, t('toast.forgotten'))
    })
  const actions = []
  if (tab === 'near') {
    actions.push(h(Btn, { key: 'p', icon: 'arrow-up', disabled: !!job || c.origin === 'untrusted', onClick: () => startJob('promote', 'promote', { key: c.key, target: vault && c.target === 'memory' ? undefined : c.target }) }, t('near.promote')))
    actions.push(h(Btn, { key: 'pin', icon: c.pinned ? 'pinned' : 'pin', tip: t('morning.pinHint'), onClick: () => write('pin', { key: c.key, pinned: !c.pinned }, c.pinned ? t('toast.unpinned') : t('toast.pinned')) }, c.pinned ? t('near.unpin') : t('near.pin')))
    actions.push(h(Btn, { key: 'f', icon: 'trash', onClick: forget }, t('near.forget')))
  } else if (tab === 'promoted') {
    actions.push(h(Btn, { key: 'f', icon: 'trash', onClick: forget }, t('morning.forget')))
    if (canAsk()) actions.push(h(Btn, { key: 'a', icon: 'comment-discussion', onClick: () => askHermes(t('ask.memory', c.text)) }, t('morning.ask')))
  } else if (tab === 'rejected') {
    actions.push(h(Btn, { key: 'u', icon: 'discard', onClick: () => write('unforget', { key: c.key }, t('toast.unforgotten')) }, t('near.unforget')))
  } else if (tab === 'skills') {
    const quotes = (c.evidence || []).map(e => `- "${e.quote}"`).join('\n')
    if (canAsk()) actions.push(h(Btn, { key: 's', icon: 'tools', variant: 'secondary', onClick: () => askHermes(t('ask.skill', c.text, quotes)) }, t('near.makeSkill')))
    actions.push(h(Btn, { key: 'd', icon: 'close', onClick: () => write('skill.dismiss', { key: c.key }, t('toast.dismissed')) }, t('near.dismiss')))
  }
  const facts = [
    t('near.sessions', (c.sessions || []).length),
    t('near.days', (c.days || []).length),
    c.last_seen_at ? t('near.seen', ago(c.last_seen_at)) : null,
    c.status === 'promoted' && c.promoted_to ? t('near.promotedTo', promotedWhere(c.promoted_to), ago(c.promoted_at)) : null
  ].filter(Boolean)
  return h(
    'article',
    { className: `smn-cand${open ? ' is-open' : ''}`, 'data-key': c.key },
    h(
      'button',
      { type: 'button', className: 'smn-cand-main', 'aria-expanded': open, onClick: onToggle },
      h('div', { className: 'smn-cand-top' },
        h('p', { className: 'smn-fact' }, c.text),
        h(Icon, { name: open ? 'chevron-up' : 'chevron-down', size: '0.8125rem', className: 'smn-caret' })),
      h('div', { className: 'smn-cand-sub' },
        tab !== 'skills' ? h(ScoreBar, { score: c.score, min: c.min_score }) : null,
        h(Pill, { color: c.kind === 'correction' ? 'var(--ui-purple)' : 'var(--ui-text-tertiary)', icon: c.kind === 'correction' ? PRINCIPLE_ICON.inversion : null }, t(`near.kind.${c.kind}`) || c.kind),
        c.reask ? h(Pill, { color: 'var(--ui-orange)', icon: 'history', tip: t('near.backAgainHint') }, t('near.backAgain')) : null,
        c.pinned ? h(Pill, { color: 'var(--ui-accent)', icon: 'pinned' }, t('near.pinned')) : null,
        c.explicit ? h(Pill, { color: 'var(--ui-green)', icon: 'pass' }, t('near.explicit')) : null,
        c.origin === 'untrusted' ? h(Pill, { color: 'var(--ui-red)', icon: 'shield' }, t('near.untrusted')) : null,
        deferred.principle ? h(PrincipleChip, { principle: deferred.principle }) : null,
        h('span', { className: 'smn-meta' }, facts.join(' · '))),
      tab === 'near' || tab === 'skills'
        ? h('div', { className: 'smn-missing-list' },
            allPass ? h('span', { className: 'smn-missing is-pass' }, h(Icon, { name: 'pass', size: '0.75rem' }), t('near.passes')) : (c.missing || []).map((m, i) => h('span', { key: i, className: 'smn-missing' }, h(Icon, { name: 'circle-large-outline', size: '0.625rem' }), m)),
            deferred.text && tab === 'near' ? h('span', { className: 'smn-missing' }, h(Icon, { name: 'clock', size: '0.6875rem' }), deferred.text) : null)
        : null
    ),
    open
      ? h(
          'div',
          { className: 'smn-cand-detail' },
          h('div', { className: 'smn-cand-cols' },
            h('div', { className: 'smn-grow' },
              c.evidence && c.evidence.length
                ? h('div', { className: 'smn-quotes' }, h('span', { className: 'smn-label' }, t('near.evidence')), c.evidence.map((e, i) => h('blockquote', { key: i, className: 'smn-quote' }, e.quote, h('span', { className: 'smn-meta' }, ` · ${shortDate(e.day)}`))))
                : null,
              c.tags && c.tags.length ? h('div', { className: 'smn-tags' }, c.tags.map(tag => h('span', { key: tag, className: 'smn-tag' }, `#${tag}`))) : null,
              h(SessionChips, { sessions: c.sessions, max: 6 })),
            h('div', { className: 'smn-cand-why' }, h(SignalBars, { components: c.components, score: c.raw ?? c.score, min: c.min_score })))
        )
      : null,
    actions.length ? h('div', { className: 'smn-row-actions smn-cand-actions' }, actions) : null
  )
}

// ==== sleep chart ===========================================================

// Depth bands of the hypnogram (y in SVG units). A night is a column that
// sinks from the surface through Light and REM into Deep; Deep grows with
// every fact kept.
const BAND = { top: 16, light: 42, rem: 66, deep: 76, keptStep: 6.5 }

function nightLevel(n) {
  if (n.quiet) return 'awake'
  if (n.promoted + n.merged > 0) return 'deep'
  if (n.themes + n.truths > 0) return 'rem'
  return 'light'
}

function SleepChart({ nights, selectedRun, onSelect }) {
  const t = useTr()
  const [hover, setHover] = useState(null)
  const list = (nights || []).slice(-30)
  const W = 340
  const H = 136
  const left = 34
  const right = 4
  const slots = 30
  const step = (W - left - right) / slots
  const offset = slots - list.length
  const x = i => left + (i + offset) * step + step / 2
  const bw = Math.max(3, step * 0.6)
  const maxFrag = Math.max(1, ...list.map(n => n.fragments))
  const kept = list.reduce((a, n) => a + n.promoted + n.merged, 0)
  const quiet = list.filter(n => n.quiet).length
  const noop = list.filter(n => n.noop).length
  const hovered = hover != null ? list[hover] : null
  const axisY = H - 4
  const column = (n, i) => {
    const level = nightLevel(n)
    const cx = x(i)
    const x0 = cx - bw / 2
    const parts = []
    if (level === 'awake') {
      parts.push(h('circle', { key: 'q', cx, cy: BAND.top, r: 1.4, fill: 'var(--ui-text-tertiary)' }))
    } else {
      // Light: opacity follows how much was read that night.
      parts.push(h('rect', { key: 'l', x: x0, y: BAND.top, width: bw, height: BAND.light - BAND.top, rx: 1.5, fill: 'var(--ui-text-primary)', opacity: 0.1 + 0.18 * (n.fragments / maxFrag) }))
      if (level === 'rem' || level === 'deep')
        parts.push(h('rect', { key: 'r', x: x0, y: BAND.light + 1, width: bw, height: BAND.rem - BAND.light - 1, rx: 1.5, fill: 'var(--ui-purple)', opacity: 0.3 + 0.12 * Math.min(4, n.themes) }))
      if (level === 'deep') {
        const k = Math.min(6, n.promoted + n.merged)
        for (let j = 0; j < k; j++)
          parts.push(h('rect', { key: `d${j}`, x: x0, y: BAND.rem + 2 + j * BAND.keptStep, width: bw, height: BAND.keptStep - 1.5, rx: 1.5, fill: 'var(--ui-accent)', opacity: j < n.promoted ? 1 : 0.6 }))
      }
      if (n.noop && !n.errors) parts.push(h('circle', { key: 'n', cx, cy: BAND.rem + 6, r: 2.4, fill: 'none', stroke: 'var(--ui-orange)', strokeWidth: 1.2 }))
    }
    if (n.errors) parts.push(h('path', { key: 'e', d: `M${cx - 2.4},${BAND.top - 11} l4.8,4.8 m0,-4.8 l-4.8,4.8`, stroke: 'var(--ui-red)', strokeWidth: 1.3, strokeLinecap: 'round' }))
    return h('g', { key: `c${i}`, opacity: hover != null && hover !== i ? 0.55 : 1 }, parts)
  }
  return h(
    Section,
    { id: 'smn-sleep', title: t('chart.title'), hint: t('chart.hint') },
    !list.length ? h(Empty, { icon: 'eye-closed' }, t('chart.empty')) : h(
      'div',
      { className: 'smn-chart', onMouseLeave: () => setHover(null) },
      h(
        'svg',
        { viewBox: `0 0 ${W} ${H}`, className: 'smn-chart-svg', role: 'img', 'aria-label': t('chart.title') },
        // Surface line + band labels
        h('line', { x1: left - 2, x2: W - right, y1: BAND.top, y2: BAND.top, stroke: 'var(--ui-stroke-secondary)' }),
        [['light', (BAND.top + BAND.light) / 2], ['rem', (BAND.light + BAND.rem) / 2], ['deep', BAND.rem + 12]].map(([stage, y]) =>
          h('text', { key: stage, x: 0, y: y + 3, className: 'smn-chart-label' }, t(`chart.stages.${stage}`))),
        h('line', { x1: left - 2, x2: W - right, y1: BAND.light + 0.5, y2: BAND.light + 0.5, stroke: 'var(--ui-stroke-quaternary)', strokeDasharray: '1 3' }),
        h('line', { x1: left - 2, x2: W - right, y1: BAND.rem + 0.5, y2: BAND.rem + 0.5, stroke: 'var(--ui-stroke-quaternary)', strokeDasharray: '1 3' }),
        // Selected / hovered night
        list.map((n, i) => (selectedRun && n.started_at === selectedRun ? h('rect', { key: `s${i}`, x: x(i) - step / 2, y: 2, width: step, height: axisY - 12, rx: 2, fill: 'var(--ui-accent)', opacity: 0.14 }) : null)),
        list.map(column),
        list.length
          ? [0, Math.floor((list.length - 1) / 2), list.length - 1].filter((v, i, a) => a.indexOf(v) === i).map(i =>
              h('text', { key: `x${i}`, x: x(i), y: axisY, textAnchor: i === list.length - 1 ? 'end' : i === 0 ? 'start' : 'middle', className: 'smn-chart-label' }, shortDate(list[i].date)))
          : h('text', { x: (W + left) / 2, y: H / 2, textAnchor: 'middle', className: 'smn-chart-label' }, t('chart.empty')),
        list.map((n, i) => h('rect', {
          key: `h${i}`,
          x: x(i) - step / 2,
          y: 0,
          width: step,
          height: axisY - 10,
          fill: 'transparent',
          className: 'smn-chart-hit',
          tabIndex: 0,
          role: 'button',
          'aria-label': `${n.date}: ${nightLevel(n)}`,
          onMouseEnter: () => setHover(i),
          onFocus: () => setHover(i),
          onBlur: () => setHover(null),
          onClick: () => onSelect(n.started_at),
          onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(n.started_at) } }
        }))
      ),
      hovered ? h(NightTip, { n: hovered, leftPct: (x(hover) / W) * 100 }) : null
    ),
    list.length
      ? h('div', { className: 'smn-chart-foot' },
          h('span', { className: 'smn-legend-item' }, h('span', { className: 'smn-swatch', style: { background: 'var(--ui-accent)' } }), t('chart.stages.deep')),
          h('span', { className: 'smn-legend-item' }, h('span', { className: 'smn-swatch', style: { background: 'color-mix(in srgb, var(--ui-purple) 60%, transparent)' } }), t('chart.stages.rem')),
          h('span', { className: 'smn-legend-item' }, h('span', { className: 'smn-ring-swatch' }), t('chart.tip.noop')),
          h('span', { className: 'smn-grow' }),
          h('span', { className: 'smn-meta' }, t('chart.summary', kept, quiet, noop)))
      : null
  )
}

function NightTip({ n, leftPct }) {
  const t = useTr()
  const lines = n.quiet
    ? [t('chart.tip.quiet')]
    : [t('chart.tip.light', n.fragments, n.observations), t('chart.tip.rem', n.themes, n.truths), t('chart.tip.deep', n.promoted, n.merged, n.deferred)]
  if (n.noop && !n.quiet) lines.push(t('chart.tip.noop'))
  if (n.errors) lines.push(t('chart.tip.errors', n.errors))
  return h(
    'div',
    { className: 'smn-tip', style: { left: `${clamp(leftPct, 22, 78)}%` }, role: 'tooltip' },
    h('strong', null, new Date(`${n.date}T12:00:00`).toLocaleDateString(locale(), { weekday: 'short', month: 'short', day: 'numeric' }), n.dry_run ? ` · ${t('chart.tip.dry')}` : ''),
    lines.map((l, i) => h('span', { key: i }, l)),
    !n.quiet ? h('span', { className: 'smn-meta' }, t('chart.tip.click')) : null
  )
}

// ==== themes ================================================================

/** Deterministic force layout (no randomness → the constellation never jumps). */
function layoutThemes(themes, links, W, H) {
  const n = themes.length
  const nodes = themes.map((th, i) => {
    const a = (i / Math.max(1, n)) * Math.PI * 2 + 0.6
    const rad = (i % 2 ? 0.34 : 0.22) * Math.min(W, H) + (1 - th.strength) * 30
    return { ...th, x: W / 2 + Math.cos(a) * rad * 1.5, y: H / 2 + Math.sin(a) * rad, vx: 0, vy: 0, r: 3 + 8 * Math.sqrt(th.strength) }
  })
  const idx = Object.fromEntries(nodes.map((nd, i) => [nd.tag, i]))
  const edges = links.filter(l => idx[l.a] != null && idx[l.b] != null)
  for (let it = 0; it < 320; it++) {
    for (let i = 0; i < n; i++)
      for (let j = i + 1; j < n; j++) {
        const A = nodes[i]
        const B = nodes[j]
        let dx = B.x - A.x
        let dy = B.y - A.y
        const d2 = Math.max(60, dx * dx + dy * dy)
        const f = 1400 / d2
        const d = Math.sqrt(d2)
        dx /= d
        dy /= d
        A.vx -= dx * f
        A.vy -= dy * f
        B.vx += dx * f
        B.vy += dy * f
      }
    for (const e of edges) {
      const A = nodes[idx[e.a]]
      const B = nodes[idx[e.b]]
      const dx = B.x - A.x
      const dy = B.y - A.y
      const d = Math.max(1, Math.hypot(dx, dy))
      const f = (d - 62) * 0.012 * Math.min(3, e.weight)
      A.vx += (dx / d) * f
      A.vy += (dy / d) * f
      B.vx -= (dx / d) * f
      B.vy -= (dy / d) * f
    }
    for (const nd of nodes) {
      nd.vx += (W / 2 - nd.x) * 0.006
      nd.vy += (H / 2 - nd.y) * 0.012
      nd.vx *= 0.82
      nd.vy *= 0.82
      nd.x = clamp(nd.x + nd.vx, nd.r + 24, W - nd.r - 24)
      nd.y = clamp(nd.y + nd.vy, nd.r + 6, H - nd.r - 16)
    }
  }
  return { nodes, edges, idx }
}

function Constellation({ data, active, onPick }) {
  const t = useTr()
  const W = 340
  const H = 210
  const themes = (data && data.themes) || []
  const links = (data && data.links) || []
  const layout = useMemo(() => layoutThemes(themes.slice(0, 16), links, W, H), [data])
  const neighbours = useMemo(() => {
    if (!active) return null
    const s = new Set([active])
    links.forEach(l => {
      if (l.a === active) s.add(l.b)
      if (l.b === active) s.add(l.a)
    })
    return s
  }, [active, links])
  let body
  if (!data) body = h(Skel, { height: 180 })
  else if (!themes.length) body = h(Empty, { icon: 'symbol-namespace' }, t('themes.empty'))
  else
    body = h(
      'svg',
      { viewBox: `0 0 ${W} ${H}`, className: 'smn-constellation', role: 'group', 'aria-label': t('themes.title') },
      layout.edges.map((e, i) => {
        const A = layout.nodes[layout.idx[e.a]]
        const B = layout.nodes[layout.idx[e.b]]
        const lit = neighbours && neighbours.has(e.a) && neighbours.has(e.b) && (e.a === active || e.b === active)
        return h('line', { key: i, x1: A.x, y1: A.y, x2: B.x, y2: B.y, stroke: lit ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)', strokeWidth: 0.6 + Math.min(4, e.weight) * 0.45, opacity: neighbours && !lit ? 0.35 : 0.9 })
      }),
      layout.nodes.map(nd => {
        const on = nd.tag === active
        const dim = neighbours && !neighbours.has(nd.tag)
        return h(
          'g',
          {
            key: nd.tag,
            className: 'smn-star',
            tabIndex: 0,
            role: 'button',
            'aria-pressed': on,
            'aria-label': `${nd.tag}, ${t('themes.count', nd.count)}`,
            opacity: dim ? 0.4 : 1,
            onClick: () => onPick(on ? null : nd.tag),
            onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(on ? null : nd.tag) } }
          },
          h('title', null, `${nd.tag} · ${t('themes.count', nd.count)}`),
          h('circle', { cx: nd.x, cy: nd.y, r: nd.r + 5, fill: 'var(--ui-accent)', opacity: on ? 0.18 : 0.07 }),
          h('circle', { cx: nd.x, cy: nd.y, r: nd.r, fill: on ? 'var(--ui-accent)' : 'color-mix(in srgb, var(--ui-accent) 30%, var(--ui-bg-editor, transparent))', stroke: 'var(--ui-accent)', strokeWidth: 1 }),
          h('text', { x: nd.x, y: nd.y + nd.r + 10, textAnchor: 'middle', className: `smn-star-label${on ? ' is-on' : ''}` }, nd.tag)
        )
      })
    )
  return h(Section, { id: 'smn-themes', title: t('themes.title'), hint: t('themes.hint') }, body)
}

// ==== diary =================================================================

function Diary() {
  const t = useTr()
  const q = useDiary()
  const [all, setAll] = useState(false)
  const entries = (q.data && q.data.entries) || []
  const shown = all ? entries : entries.slice(0, 2)
  let body
  if (q.isLoading) body = h('div', { className: 'smn-diary' }, h(Skel, { height: 12, width: 140 }), h(Skel, { height: 60, style: { marginTop: 10 } }))
  else if (!entries.length) body = h(Empty, { icon: 'book' }, t('diary.empty'))
  else
    body = h(
      'div',
      { className: 'smn-diary' },
      shown.map((e, i) =>
        h('article', { key: `${e.date}${i}`, className: `smn-diary-entry${e.fallback ? ' is-fallback' : ''}` },
          h('h3', { className: 'smn-diary-date' }, e.date),
          h('div', { className: 'smn-diary-text smn-selectable' }, Streamdown ? h(Streamdown, null, e.text) : String(e.text).split(/\n{2,}/).map((p, k) => h('p', { key: k }, p))),
          e.fallback ? h('p', { className: 'smn-meta' }, t('diary.fallback')) : null)
      ),
      entries.length > 2 ? h('div', { className: 'smn-more' }, h(Btn, { icon: all ? 'chevron-up' : 'chevron-down', onClick: () => setAll(!all) }, all ? t('common.showLess') : t('common.showMore', entries.length - 2))) : null,
      q.data && q.data.path ? h('p', { className: 'smn-meta smn-mono' }, q.data.path) : null
    )
  return h(Section, { id: 'smn-diary', title: t('diary.title'), hint: t('diary.hint') }, body)
}

// ==== calibration ===========================================================

function Stepper({ label, value, min, max, step, format, onChange, disabled }) {
  const fmt = format || (v => String(v))
  const set = v => onChange(Math.round(clamp(v, min, max) / step) * step)
  return h(
    'div',
    { className: 'smn-stepper' },
    h('span', { className: 'smn-stepper-label' }, label),
    h('div', { className: 'smn-stepper-ctl' },
      h(Btn, { icon: 'remove', 'aria-label': `${label} −`, disabled: disabled || value <= min, onClick: () => set(value - step) }),
      h('span', { className: 'smn-stepper-val' }, fmt(value)),
      h(Btn, { icon: 'add', 'aria-label': `${label} +`, disabled: disabled || value >= max, onClick: () => set(value + step) }))
  )
}

function Calibration({ ov }) {
  const t = useTr()
  const q = useStats()
  const { write, pending } = useWrite()
  const s = q.data
  const settings = (ov && ov.settings) || {}
  if (!s) return null
  const max = Math.max(1, ...s.histogram.map(b => b.count))
  const W = 300
  const H = 64
  const bw = W / s.histogram.length
  const gates = Object.entries(s.by_gate || {}).filter(([g, n]) => n > 0 && g !== 'not_promoted' && g !== 'not_rejected').sort((a, b) => b[1] - a[1])
  const maxGate = Math.max(1, ...gates.map(g => g[1]))
  return h(
    'details',
    { className: 'smn-details', id: 'smn-calibration' },
    h('summary', { className: 'smn-summary' }, h(Icon, { name: 'chevron-right', size: '0.75rem', className: 'smn-summary-caret' }), h('span', { className: 'smn-h2' }, t('calib.title'))),
    h(
      'div',
      { className: 'smn-calib' },
      h('p', { className: 'smn-meta' }, t('calib.hint', s.candidates_total)),
      h(
        'svg',
        { viewBox: `0 0 ${W} ${H + 14}`, className: 'smn-hist', role: 'img', 'aria-label': t('calib.title') },
        s.histogram.map((b, i) => {
          const hgt = (b.count / max) * H
          const pass = b.bucket + 0.0001 >= s.min_score - 0.025
          return h('rect', { key: i, x: i * bw + 1, y: H - hgt, width: bw - 2, height: Math.max(b.count ? 2 : 0.5, hgt), rx: 1, fill: pass ? 'var(--ui-accent)' : 'var(--ui-text-quaternary)' }, h('title', null, `${f2(b.bucket)}–${f2(b.bucket + 0.05)}: ${b.count}`))
        }),
        h('line', { x1: s.min_score * W, x2: s.min_score * W, y1: 0, y2: H + 2, stroke: 'var(--ui-text-primary)', strokeWidth: 1, strokeDasharray: '2 2' }),
        h('text', { x: s.min_score * W + 3, y: 8, className: 'smn-chart-label' }, f2(s.min_score)),
        h('text', { x: 0, y: H + 12, className: 'smn-chart-label' }, '0'),
        h('text', { x: W, y: H + 12, textAnchor: 'end', className: 'smn-chart-label' }, '1')
      ),
      gates.length
        ? h('div', { className: 'smn-gates' },
            h('span', { className: 'smn-label' }, t('calib.blockedBy')),
            gates.map(([g, n]) => h('div', { key: g, className: 'smn-gate' },
              h('span', { className: 'smn-gate-name' }, t(`calib.gates.${g}`) || g),
              h('span', { className: 'smn-gate-bar' }, h('span', { style: { width: `${(n / maxGate) * 100}%` } })),
              h('span', { className: 'smn-gate-n' }, n))))
        : null,
      settings.min_score != null
        ? h(Stepper, { label: t('calib.minScore'), value: settings.min_score, min: 0.4, max: 0.95, step: 0.05, format: f2, disabled: pending, onChange: v => write('settings.set', { key: 'min_score', value: Number(v.toFixed(2)) }, t('thinks.saved')) })
        : null,
      settings.min_sessions != null
        ? h(Stepper, { label: t('calib.minSessions'), value: settings.min_sessions, min: 1, max: 5, step: 1, disabled: pending, onChange: v => write('settings.set', { key: 'min_sessions', value: v }, t('thinks.saved')) })
        : null
    )
  )
}

// ==== principles ("How Somnia thinks") =====================================

function Thinks({ ov }) {
  const t = useTr()
  const { write, pending } = useWrite()
  if (!ov || !ov.principles) return null
  serverPrinciples = ov.principles
  const settings = ov.settings || {}
  const all = ov.principles.all && ov.principles.all.length ? ov.principles.all : PRINCIPLES
  const enabled = settings.principles || ov.principles.active || []
  const toggle = (id, on) => {
    const next = all.filter(p => (p === id ? on : enabled.includes(p)))
    write('settings.set', { key: 'principles', value: next }, t('thinks.saved'))
  }
  return h(
    Section,
    { id: 'smn-thinks', title: t('thinks.title'), hint: t('thinks.hint') },
    h(
      'div',
      { className: 'smn-thinks' },
      all.map(id => {
        const on = enabled.includes(id)
        const label = principleText(t, id, 'label') || id
        const desc = principleText(t, id, 'description')
        const switchId = `smn-p-${id}`
        return h(
          'div',
          { key: id, className: `smn-principle${on ? ' is-on' : ''}` },
          h(Icon, { name: PRINCIPLE_ICON[id] || 'law', size: '0.875rem', className: 'smn-principle-icon' }),
          h('label', { className: 'smn-principle-text', htmlFor: switchId },
            h('span', { className: 'smn-principle-label' }, label, OPT_IN_PRINCIPLES.includes(id) ? h(Tooltip, { label: t('thinks.optInHint') }, h('span', { className: 'smn-optin' }, t('thinks.optIn'))) : null),
            desc ? h('span', { className: 'smn-meta' }, desc) : null),
          Switch
            ? h(Switch, { id: switchId, size: 'xs', checked: on, disabled: pending, onCheckedChange: v => toggle(id, !!v), 'aria-label': label })
            : h('input', { id: switchId, type: 'checkbox', checked: on, disabled: pending, onChange: e => toggle(id, e.target.checked) })
        )
      }),
      h('div', { className: 'smn-thinks-controls' },
        settings.destructive
          ? h('div', { className: 'smn-stepper' },
              h('span', { className: 'smn-stepper-label' }, t('thinks.destructive')),
              SegmentedControl
                ? h(SegmentedControl, { options: [{ id: 'apply', label: t('thinks.apply') }, { id: 'stage', label: t('thinks.stage') }], value: settings.destructive, sizing: 'content', disabled: pending, onChange: v => v !== settings.destructive && write('settings.set', { key: 'destructive', value: v }, t('thinks.saved')) })
                : h('select', { value: settings.destructive, onChange: e => write('settings.set', { key: 'destructive', value: e.target.value }, t('thinks.saved')) }, h('option', { value: 'apply' }, t('thinks.apply')), h('option', { value: 'stage' }, t('thinks.stage'))))
          : null,
        settings.max_promotions != null
          ? h(Stepper, { label: t('thinks.maxPromotions'), value: settings.max_promotions, min: 1, max: 10, step: 1, disabled: pending, onChange: v => write('settings.set', { key: 'max_promotions', value: v }, t('thinks.saved')) })
          : null)
    )
  )
}

// ==== dialogs ===============================================================

function PreviewDialog() {
  const t = useTr()
  const result = useStore($preview)
  if (!result) return null
  const changes = result.changes || result.plan || []
  const close = () => $preview.set(null)
  const content = [
    h('div', { key: 'list', className: 'smn-preview' },
      result.summary ? h('p', { className: 'smn-body' }, result.summary) : null,
      changes.length
        ? changes.map((c, i) => {
            const r = splitPrinciple(c.reason)
            return h('div', { key: i, className: 'smn-preview-row' },
              h('div', { className: 'smn-change-meta' },
                h(Pill, { color: ACTION_COLOR[c.action], icon: ACTION_ICON[c.action], strong: true }, t(`morning.action.${c.action}`)),
                h('span', { className: 'smn-meta smn-mono' }, t(`morning.target.${c.target}`)),
                c.score != null && c.action !== 'evicted' ? h('span', { className: 'smn-meta' }, f2(c.score)) : null,
                r.principle ? h(PrincipleChip, { principle: r.principle }) : null),
              c.action === 'evicted' ? h(Diff, { removed: [c.text] }) : c.replaced && c.replaced.length ? h(Diff, { removed: c.replaced, added: c.text }) : h('p', { className: 'smn-fact' }, c.text),
              r.text ? h('p', { className: 'smn-reason' }, r.text) : null)
          })
        : h(Empty, { icon: 'eye-closed' }, t('preview.none')))
  ]
  const footer = [
    h(Btn, { key: 'c', size: 'sm', onClick: close }, t('common.close')),
    changes.length ? h(Btn, { key: 'r', size: 'sm', variant: 'default', icon: 'sparkle', onClick: () => { close(); startJob('run', 'run', {}) } }, t('preview.run')) : null
  ]
  if (Dialog && DialogContent) {
    return h(
      Dialog,
      { open: true, onOpenChange: v => !v && close() },
      h(DialogContent, { className: 'smn-dialog' },
        h(DialogHeader, null, h(DialogTitle, null, t('preview.title')), DialogDescription ? h(DialogDescription, null, t('header.previewHint')) : null),
        ...content,
        DialogFooter ? h(DialogFooter, null, ...footer) : h('div', { className: 'smn-row-actions smn-end' }, ...footer))
    )
  }
  return h('div', { className: 'smn-modal-backdrop', role: 'dialog', 'aria-modal': true },
    h('div', { className: 'smn-modal smn-dialog' }, h('h3', { className: 'smn-h2' }, t('preview.title')), ...content, h('div', { className: 'smn-row-actions smn-end' }, ...footer)))
}

// ==== page ==================================================================

function Disabled({ onRetry }) {
  const t = useTr()
  return h(
    'div',
    { className: 'smn-page' },
    h('div', { className: 'smn-center' },
      h(Empty, { icon: 'eye-closed', title: t('disabled.title'), action: h(Btn, { size: 'sm', variant: 'secondary', icon: 'refresh', onClick: onRetry }, t('common.retry')) },
        h('p', null, t('disabled.body')),
        h('code', { className: 'smn-code smn-selectable' }, t('disabled.command'))))
  )
}

function Page() {
  const t = useTr()
  const open = useGatewayOpen()
  const overview = useOverview()
  const invalidate = useInvalidate()
  const [run, setRun] = useState(null) //   selected night (null = latest)
  const [theme, setTheme] = useState(null)
  const [confirmNode, confirm] = useConfirm()
  const { write } = useWrite()
  const latest = useChanges(null)
  const themes = useThemes()
  const spaceRef = useRef(null)
  const nearRef = useRef(null)
  const ov = overview.data

  // Mark the latest run as seen (hides the statusbar "new" chip).
  useEffect(() => {
    if (ov && ov.last_run && ov.last_run.at !== $seenRun.get()) {
      $seenRun.set(ov.last_run.at)
      remember('seen_run', ov.last_run.at)
    }
  }, [ov && ov.last_run && ov.last_run.at])

  const latestKeys = useMemo(() => new Set(((latest.data && latest.data.changes) || []).filter(c => c.key && c.action !== 'deferred' && c.action !== 'skipped').map(c => c.key)), [latest.data])
  const canUndo = ((latest.data && latest.data.changes) || []).some(c => c.undoable)

  const onUndo = () =>
    confirm({
      title: t('confirm.undoTitle'),
      description: t('confirm.undoBody'),
      confirmLabel: t('confirm.undoLabel'),
      destructive: true,
      onConfirm: async () => {
        const res = await rpc('undo', {})
        invalidate()
        toast('success', t('toast.undone', (res && res.restored && res.restored.join(', ')) || 'memory'))
      }
    })
  const scrollTo = ref => ref.current && ref.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
  const onReview = alarm => scrollTo(alarm.kind === 'budget_full' ? spaceRef : nearRef)
  const pickTheme = tag => {
    setTheme(tag)
    if (tag) scrollTo(nearRef)
  }

  if (overview.error && isDisabledError(overview.error)) return h(Disabled, { onRetry: () => overview.refetch() })
  if (!open)
    return h('div', { className: 'smn-page' }, h('div', { className: 'smn-center' }, h(Empty, { icon: 'loading', title: t('gateway.waiting') })))

  return h(
    'div',
    { className: 'smn-page' },
    h(JobWatcher),
    h(
      'div',
      { className: 'smn-wrap' },
      h(Header, { ov, canUndo, onUndo }),
      overview.error ? h('div', { className: 'smn-alarm', style: { '--c': 'var(--ui-red)' } }, h(Icon, { name: 'error' }), h('div', { className: 'smn-alarm-text' }, h('p', { className: 'smn-alarm-title' }, t('error.load')), h('p', { className: 'smn-body' }, errorText(overview.error))), h(Btn, { size: 'sm', variant: 'secondary', onClick: () => overview.refetch() }, t('common.retry'))) : null,
      ov ? h(AlarmBanner, { alarm: ov.alarm, onReview }) : null,
      h(
        'div',
        { className: 'smn-grid' },
        h('div', { className: 'smn-col smn-col-main' },
          h(MorningSection, { ov, run, onBack: () => setRun(null), confirm }),
          h(AlmostThere, { ov, theme, onClearTheme: () => setTheme(null), themes: themes.data && themes.data.themes, confirm, sectionRef: nearRef }),
          h(Diary)),
        h('div', { className: 'smn-col smn-col-side' },
          h(MemorySpace, { ov, latestKeys, confirm, focusRef: spaceRef }),
          ov ? h(SleepChart, { nights: ov.nights, selectedRun: run, onSelect: r => { setRun(r === (ov.last_run && ov.last_run.at) ? null : r); window.requestAnimationFrame(() => { const el = document.getElementById('smn-morning'); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }) }) } }) : h(Section, { title: t('chart.title') }, h(Skel, { height: 130 })),
          h(Constellation, { data: themes.data, active: theme, onPick: pickTheme }),
          h(Thinks, { ov }),
          h(Calibration, { ov }))
      )
    ),
    h(PreviewDialog),
    confirmNode,
    null && write
  )
}

// ==== surfaces ==============================================================

/** Statusbar: "✦ N new" after a night that changed something, until the page is seen. */
function StatusChip() {
  const t = useTr()
  const { data } = useOverview()
  const seen = useStore($seenRun)
  if (!data || !data.last_run || data.last_run.dry_run) return null
  if (nowS() - data.last_run.at > DAY_S || seen === data.last_run.at) return null
  const night = (data.nights || [])[data.nights.length - 1]
  const n = night ? night.promoted + night.merged : 0
  if (!n) return null
  return h(
    Tooltip,
    { label: t('surfaces.statusTip', n) },
    h('button', { type: 'button', className: 'smn-status', onClick: () => go(ROUTE) }, h(Icon, { name: 'sparkle', size: '0.7rem' }), h('span', null, t('surfaces.statusNew', n)))
  )
}

/** Session row: a tiny sparkle on chats that fed a memory. */
function SessionMark({ sessionId }) {
  const t = useTr()
  const { data } = useFed()
  const keys = data && data.sessions && data.sessions[sessionId]
  if (!keys || !keys.length) return null
  return h(Tooltip, { label: t('surfaces.fed', keys.length) }, h('span', { className: 'smn-fed', 'aria-label': t('surfaces.fed', keys.length) }, h(Icon, { name: 'sparkle', size: '0.625rem' })))
}

/** Empty chat: one quiet line about last night. */
function ChatEmptyLine() {
  const t = useTr()
  const ov = useOverview().data
  const fresh = !!(ov && ov.last_run && nowS() - ov.last_run.at < 1.5 * DAY_S)
  const q = useChanges(null)
  if (!fresh || !q.data) return null
  const first = (q.data.changes || []).find(c => ['added', 'merged', 'superseded', 'vault'].includes(c.action))
  if (!first) return null
  return h(
    'div',
    { className: 'smn-chat-empty' },
    h('button', { type: 'button', className: 'smn-chat-empty-line', onClick: () => go(ROUTE) },
      h(Icon, { name: 'sparkle', size: '0.75rem' }),
      h('span', { className: 'smn-meta' }, t('surfaces.emptyChat')),
      h('span', { className: 'smn-chat-empty-text' }, first.text))
  )
}

/** ::somnia-memory{key="claim:…"} → compact candidate card inside the transcript. */
function DirectiveCard({ attrs, streaming }) {
  const t = useTr()
  const key = attrs && attrs.key
  const q = useCandidate(key, !streaming)
  const { write } = useWrite()
  const [gone, setGone] = useState(false)
  if (!key) return null
  if (streaming || q.isLoading) return h('div', { className: 'smn-card' }, h(Skel, { height: 12, width: '30%' }), h(Skel, { height: 14, style: { marginTop: 8 } }))
  if (q.error) return h('div', { className: 'smn-card smn-card-muted' }, h(Icon, { name: 'eye-closed', size: '0.8125rem' }), h('span', { className: 'smn-meta' }, isDisabledError(q.error) ? t('disabled.title') : t('surfaces.missing')))
  const c = q.data
  if (gone || c.status === 'rejected') return h('div', { className: 'smn-card smn-card-muted' }, h(Icon, { name: 'circle-slash', size: '0.8125rem' }), h('span', { className: 'smn-meta' }, t('surfaces.gone')))
  const status = c.status === 'promoted' ? ['var(--ui-green)', 'pass', promotedWhere(c.promoted_to)] : c.status === 'skill' ? ['var(--ui-cyan)', 'tools', t('near.tabs.skills')] : ['var(--ui-text-tertiary)', 'telescope', t('near.title')]
  return h(
    'div',
    { className: 'smn-card', 'data-key': c.key },
    h('div', { className: 'smn-card-head' },
      h(Icon, { name: 'sparkle', size: '0.75rem', className: 'smn-accent' }),
      h('span', { className: 'smn-label' }, t('surfaces.card')),
      h(Pill, { color: status[0], icon: status[1] }, status[2]),
      c.reask ? h(Pill, { color: 'var(--ui-orange)', icon: 'history', tip: t('near.backAgainHint') }, t('near.backAgain')) : null,
      c.pinned ? h(Pill, { color: 'var(--ui-accent)', icon: 'pinned' }, t('near.pinned')) : null),
    h('div', { className: 'smn-card-body' },
      h(ScoreRing, { score: c.score, min: c.min_score, size: 42 }),
      h('div', { className: 'smn-grow' },
        h('p', { className: 'smn-fact' }, c.text),
        c.status !== 'promoted' && c.missing && c.missing.length ? h('div', { className: 'smn-missing-list' }, c.missing.slice(0, 2).map((m, i) => h('span', { key: i, className: 'smn-missing' }, h(Icon, { name: 'circle-large-outline', size: '0.625rem' }), m))) : null,
        h(SessionChips, { sessions: c.sessions, max: 3 }))),
    h(SignalBars, { components: c.components, score: c.raw ?? c.score, min: c.min_score, compact: true }),
    h('div', { className: 'smn-row-actions' },
      h(ArmedButton, { icon: 'trash', label: t('morning.forget'), armedLabel: t('surfaces.reallyForget'), onConfirm: () => write('forget', { key: c.key, remove_entry: c.status === 'promoted' }).then(() => setGone(true), () => {}) }),
      c.status !== 'promoted' ? h(Btn, { icon: c.pinned ? 'pinned' : 'pin', tip: t('morning.pinHint'), onClick: () => write('pin', { key: c.key, pinned: !c.pinned }, c.pinned ? t('toast.unpinned') : t('toast.pinned')) }, c.pinned ? t('near.unpin') : t('near.pin')) : null,
      h(Btn, { icon: 'link-external', onClick: () => { $focusKey.set(c.key); go(ROUTE) } }, t('surfaces.open')))
  )
}

// ==== styles ================================================================
// The app's Tailwind build never sees this file, so the page carries its own
// rules. Theme variables only; color-mix() for tints.

const STYLESHEET = `
.smn-page{height:100%;overflow-y:auto;overflow-x:hidden;container-type:inline-size;container-name:smn;color:var(--ui-text-primary);font-size:0.8125rem;line-height:1.5;user-select:text;-webkit-user-select:text}
.smn-page *{box-sizing:border-box}
.smn-wrap{max-width:1240px;margin:0 auto;padding:0 28px 56px;display:flex;flex-direction:column;gap:20px}
.smn-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:36px;margin-top:4px}
@container smn (min-width: 940px){.smn-grid{grid-template-columns:minmax(0,1fr) 340px;gap:44px}}
@container smn (max-width: 560px){.smn-wrap{padding:0 16px 40px}.smn-header{flex-direction:column;align-items:flex-start}.smn-change{grid-template-columns:38px minmax(0,1fr)}.smn-change-why{grid-column:2}}
.smn-col{display:flex;flex-direction:column;gap:36px;min-width:0}
.smn-grow{flex:1 1 auto;min-width:0}
.smn-center{display:grid;place-items:center;min-height:60%;padding:48px 24px}
.smn-mono{font-family:var(--dt-font-mono,ui-monospace,monospace);font-size:0.6875rem}
.smn-selectable{user-select:text;-webkit-user-select:text}
.smn-meta{font-size:0.6875rem;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.smn-label{font-size:0.6875rem;font-weight:600;color:var(--ui-text-secondary)}
.smn-body{margin:0;color:var(--ui-text-secondary);font-size:0.8125rem;line-height:1.55}
.smn-bad{color:var(--ui-red)}
.smn-warn{color:var(--ui-orange)}
.smn-accent{color:var(--ui-accent)}
.smn-end{justify-content:flex-end}
.smn-danger,.smn-danger:hover{color:var(--ui-red)!important}

/* header */
.smn-header{position:relative;display:flex;align-items:center;gap:16px;padding:26px 0 20px;border-bottom:1px solid var(--ui-stroke-tertiary);isolation:isolate}
.smn-stars{position:absolute;inset:0 -28px;width:calc(100% + 56px);height:100%;z-index:-1;pointer-events:none;-webkit-mask-image:linear-gradient(90deg,transparent 0%,black 30%,black 70%,transparent 100%);mask-image:linear-gradient(90deg,transparent 0%,black 30%,black 70%,transparent 100%);opacity:.7}
.smn-header-main{display:flex;align-items:flex-start;gap:12px;flex:1 1 auto;min-width:0}
.smn-moon{flex:none;margin-top:1px}
.smn-header-text{display:flex;flex-direction:column;gap:3px;min-width:0}
.smn-h1{margin:0;font-size:1.125rem;line-height:1.3;font-weight:600;letter-spacing:-0.01em;color:var(--ui-text-primary)}
.smn-state{margin:0;display:flex;align-items:center;flex-wrap:wrap;gap:6px;color:var(--ui-text-secondary);font-size:0.75rem}
.smn-state-model{display:inline-flex;color:var(--ui-text-tertiary);cursor:help}
.smn-dot{width:6px;height:6px;border-radius:50%;background:var(--ui-text-quaternary);flex:none}
.smn-dot.is-ok{background:var(--ui-green)}
.smn-dot.is-warn{background:var(--ui-yellow)}
.smn-progress{margin:2px 0 0;display:flex;align-items:center;gap:6px;font-size:0.75rem;color:var(--ui-accent)}
.smn-header-actions{display:flex;align-items:center;gap:4px;flex-wrap:wrap}

/* sections */
.smn-section{display:flex;flex-direction:column;gap:12px;min-width:0;scroll-margin-top:16px}
.smn-section-head{display:flex;align-items:flex-end;gap:12px}
.smn-section-titles{flex:1 1 auto;min-width:0}
.smn-section-actions{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
.smn-h2{margin:0;font-size:0.875rem;font-weight:600;line-height:1.35;color:var(--ui-text-primary)}
.smn-hint{margin:2px 0 0;font-size:0.75rem;color:var(--ui-text-tertiary)}

/* empty + first night */
.smn-empty{display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px;padding:28px 20px;border:1px dashed var(--ui-stroke-tertiary);border-radius:8px;color:var(--ui-text-tertiary)}
.smn-empty-icon{color:var(--ui-text-quaternary)}
.smn-empty-title{margin:0;font-size:0.8125rem;font-weight:600;color:var(--ui-text-primary)}
.smn-empty-body{max-width:46ch;font-size:0.75rem;line-height:1.55}
.smn-empty-body p{margin:0 0 8px}
.smn-empty-action{margin-top:6px}
.smn-first{display:flex;gap:16px;align-items:flex-start;padding:22px 22px;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;background:color-mix(in srgb,var(--ui-accent) 4%,transparent)}
.smn-first .smn-body{margin:4px 0 12px;max-width:60ch}
.smn-first .smn-row-actions{margin-left:0;gap:6px}
.smn-code{display:inline-block;margin-top:2px;padding:4px 8px;border-radius:4px;background:var(--ui-bg-quaternary);font-family:var(--dt-font-mono,ui-monospace,monospace);font-size:0.75rem;color:var(--ui-text-primary)}

/* lists */
.smn-list{display:flex;flex-direction:column;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;overflow:hidden;background:color-mix(in srgb,var(--ui-bg-card,transparent) 55%,transparent)}
.smn-list>*+*{border-top:1px solid var(--ui-stroke-tertiary)}
.smn-more{display:flex;justify-content:center}
.smn-row-actions{display:flex;flex-wrap:wrap;gap:2px;align-items:center;margin-left:-8px}
.smn-end.smn-row-actions{margin-left:0}

/* pills + chips */
.smn-pill{display:inline-flex;align-items:center;gap:3px;height:18px;padding:0 6px;border-radius:3px;font-size:0.65625rem;font-weight:500;line-height:1;white-space:nowrap;color:color-mix(in srgb,var(--c) 72%,var(--ui-text-primary));background:color-mix(in srgb,var(--c) 11%,transparent)}
.smn-pill-strong{font-weight:600;background:color-mix(in srgb,var(--c) 15%,transparent)}
.smn-chips{display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.smn-chip{display:inline-flex;align-items:center;gap:4px;max-width:220px;height:20px;padding:0 7px;border:0;border-radius:10px;background:var(--ui-bg-quaternary);color:var(--ui-text-secondary);font:inherit;font-size:0.6875rem;cursor:pointer;transition:background-color .12s,color .12s}
.smn-chip:hover:not(:disabled){background:var(--ui-control-hover-background,var(--chrome-action-hover));color:var(--ui-text-primary)}
.smn-chip:disabled{cursor:default}
.smn-chip-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.smn-tag{font-size:0.6875rem;color:var(--ui-text-tertiary)}
.smn-tags{display:flex;flex-wrap:wrap;gap:8px}
.smn-page :focus-visible{outline:2px solid color-mix(in srgb,var(--ui-accent) 70%,transparent);outline-offset:1px;border-radius:3px}

/* change cards (This morning) */
.smn-change{display:grid;grid-template-columns:38px minmax(0,1fr) 188px;gap:4px 16px;padding:16px 18px 12px;align-items:start}
.smn-change.is-kept{opacity:.6}
.smn-change-body{display:flex;flex-direction:column;gap:8px;min-width:0}
.smn-change-meta{display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.smn-change-from{display:flex;align-items:flex-start;gap:8px}
.smn-change-from>.smn-meta{line-height:20px;flex:none}
.smn-change-from>.smn-chips{flex:1 1 auto;min-width:0}
.smn-change-why{padding-top:2px}
.smn-evicted-mark{display:grid;place-items:center;width:38px;height:38px;border-radius:50%;border:1.5px dashed color-mix(in srgb,var(--ui-red) 55%,transparent);color:var(--ui-red)}
.smn-fact{margin:0;font-size:0.8125rem;line-height:1.5;color:var(--ui-text-primary);overflow-wrap:anywhere}
.smn-reason{margin:0;font-size:0.75rem;line-height:1.5;color:var(--ui-text-secondary)}
.smn-ring{flex:none;display:block}
.smn-ring-text{font-size:10px;font-weight:600;fill:var(--ui-text-primary);font-variant-numeric:tabular-nums}

/* diff */
.smn-diff{display:flex;flex-direction:column;border-radius:5px;overflow:hidden;font-size:0.78125rem;line-height:1.5}
.smn-diff-line{display:grid;grid-template-columns:16px 1fr;gap:2px;padding:4px 10px 4px 6px}
.smn-diff-line.is-del{background:var(--ui-diff-remove-background);color:color-mix(in srgb,var(--ui-diff-remove-foreground,var(--ui-red)) 75%,var(--ui-text-secondary));text-decoration:line-through;text-decoration-color:color-mix(in srgb,var(--ui-red) 40%,transparent)}
.smn-diff-line.is-add{background:var(--ui-diff-add-background);color:var(--ui-text-primary)}
.smn-diff-sign{text-decoration:none;display:inline-block;font-family:var(--dt-font-mono,monospace);color:var(--ui-text-tertiary)}
.is-add .smn-diff-sign{color:var(--ui-diff-add-foreground,var(--ui-green))}
.is-del .smn-diff-sign{color:var(--ui-diff-remove-foreground,var(--ui-red))}

/* signals */
.smn-signals{display:flex;flex-direction:column;gap:3px;min-width:0}
.smn-signal{display:grid;grid-template-columns:72px minmax(0,1fr) 26px;align-items:center;gap:6px;font-size:0.65625rem;color:var(--ui-text-tertiary);cursor:help;border-radius:2px}
.smn-signal:hover{color:var(--ui-text-secondary)}
.smn-signal-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.smn-signal-track{display:block;height:6px}
.smn-signal-cap{display:block;height:100%;border-radius:2px;background:color-mix(in srgb,var(--ui-base,var(--ui-text-primary)) 9%,transparent);overflow:hidden}
.smn-signal-fill{display:block;height:100%;background:color-mix(in srgb,var(--ui-accent) 75%,transparent);border-radius:2px}
.smn-signal-val{text-align:right;font-variant-numeric:tabular-nums;color:var(--ui-text-secondary)}
.smn-signal-foot{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px}
.smn-bonus{display:inline-flex;align-items:center;gap:3px;font-size:0.65625rem;color:var(--ui-text-secondary);padding:1px 5px;border-radius:3px;background:var(--ui-bg-quaternary);cursor:help}
.smn-bonus-principle{color:color-mix(in srgb,var(--ui-purple) 70%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-purple) 12%,transparent)}
.smn-signal-total{margin-left:auto;font-size:0.6875rem;font-weight:600;font-variant-numeric:tabular-nums}
.smn-signals-compact .smn-signal{grid-template-columns:66px minmax(0,1fr) 24px}

/* score bar */
.smn-scorebar{display:inline-flex;align-items:center;gap:6px;flex:none}
.smn-scorebar-track{position:relative;width:84px;height:5px;border-radius:3px;background:color-mix(in srgb,var(--ui-base,var(--ui-text-primary)) 10%,transparent)}
.smn-scorebar-fill{position:absolute;inset:0 auto 0 0;border-radius:3px;background:var(--ui-text-tertiary)}
.smn-scorebar-fill.is-pass{background:var(--ui-accent)}
.smn-scorebar-min{position:absolute;top:-3px;bottom:-3px;width:1.5px;background:var(--ui-text-primary);opacity:.6;border-radius:1px}
.smn-scorebar-val{font-size:0.6875rem;font-weight:600;font-variant-numeric:tabular-nums;color:var(--ui-text-secondary)}

/* alarm */
.smn-alarm{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-radius:8px;border:1px solid color-mix(in srgb,var(--c) 35%,transparent);background:color-mix(in srgb,var(--c) 8%,transparent)}
.smn-alarm-icon{color:var(--c);margin-top:1px}
.smn-alarm-text{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:2px}
.smn-alarm-title{margin:0;font-weight:600;font-size:0.8125rem}
.smn-alarm-best{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:6px}
.smn-alarm-best-text{font-size:0.75rem;color:var(--ui-text-primary)}

/* memory space */
.smn-space{display:flex;flex-direction:column;gap:16px}
.smn-cap{display:flex;flex-direction:column;gap:6px}
.smn-cap-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.smn-cap-file{color:var(--ui-text-secondary);font-weight:600}
.smn-cap-num{font-size:0.75rem;font-weight:600;font-variant-numeric:tabular-nums}
.smn-cap-bar{position:relative;display:flex;gap:1.5px;height:26px;padding:0;border-radius:5px;background:color-mix(in srgb,var(--ui-base,var(--ui-text-primary)) 3%,transparent);box-shadow:inset 0 0 0 1px var(--ui-stroke-tertiary)}
.smn-seg{flex:none;min-width:3px;height:100%;border:0;padding:0;cursor:pointer;border-radius:2px;transition:filter .12s,transform .12s,box-shadow .12s}
.smn-seg:first-child{border-radius:5px 2px 2px 5px}
.smn-seg.is-human{background:color-mix(in srgb,var(--ui-base,var(--ui-text-primary)) 20%,transparent)}
.smn-seg.is-somnia{background:color-mix(in srgb,var(--ui-accent) 62%,transparent)}
.smn-seg.is-somnia.is-new{background:var(--ui-accent)}
.smn-seg.is-stale{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--ui-yellow) 70%,transparent) 0 3px,color-mix(in srgb,var(--ui-yellow) 30%,transparent) 3px 6px)}
.smn-seg.is-pointer{background:color-mix(in srgb,var(--ui-purple) 65%,transparent)}
.smn-seg:hover{filter:brightness(1.12) saturate(1.1)}
.smn-seg.is-selected{box-shadow:0 0 0 2px var(--ui-text-primary);z-index:1}
.smn-cap-goal{position:absolute;top:-4px;bottom:-4px;width:0;border-left:1.5px dashed color-mix(in srgb,var(--ui-text-primary) 45%,transparent);pointer-events:none}
.smn-cap-scale{position:relative;height:12px;font-size:0.625rem;color:var(--ui-text-tertiary)}
.smn-cap-scale span{position:absolute;transform:translateX(-50%);top:-2px}
.smn-legend{display:flex;flex-wrap:wrap;gap:12px;font-size:0.6875rem;color:var(--ui-text-tertiary)}
.smn-legend-item{display:inline-flex;align-items:center;gap:5px}
.smn-swatch{display:inline-block;width:9px;height:9px;border-radius:2px;flex:none}
.smn-swatch.is-somnia{background:color-mix(in srgb,var(--ui-accent) 62%,transparent)}
.smn-swatch.is-human{background:color-mix(in srgb,var(--ui-base,var(--ui-text-primary)) 20%,transparent)}
.smn-swatch.is-stale{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--ui-yellow) 70%,transparent) 0 2px,color-mix(in srgb,var(--ui-yellow) 30%,transparent) 2px 4px)}
.smn-swatch.is-pointer{background:color-mix(in srgb,var(--ui-purple) 65%,transparent)}
.smn-pressure{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:flex-start;font-size:0.75rem;line-height:1.45;color:var(--ui-text-secondary);padding:8px 10px;border-radius:6px;background:color-mix(in srgb,var(--ui-orange) 8%,transparent)}
.smn-pressure>i,.smn-pressure>.codicon{color:var(--ui-orange);margin-top:2px}
.smn-pressure>span{flex:1 1 calc(100% - 24px)}
.smn-pressure>button{margin-left:14px}
.smn-space-pick{margin:0}
.smn-detail{display:flex;flex-direction:column;gap:10px;padding:12px 14px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-bg-card,transparent)}
.smn-detail-head{display:flex;align-items:center;gap:8px;margin:-4px -6px -4px 0}
.smn-note{margin:0;display:flex;gap:6px;align-items:flex-start;font-size:0.75rem;color:var(--ui-text-secondary)}
.smn-note>*:first-child{margin-top:2px}
.smn-note.is-warn{color:color-mix(in srgb,var(--ui-yellow) 70%,var(--ui-text-primary))}
.smn-meta-grid{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0;font-size:0.6875rem}
.smn-meta-grid dt{color:var(--ui-text-tertiary)}
.smn-meta-grid dd{margin:0;color:var(--ui-text-secondary);font-variant-numeric:tabular-nums}

/* editor */
.smn-editor{display:flex;flex-direction:column;gap:6px}
.smn-textarea{width:100%;resize:vertical;font:inherit;font-size:0.8125rem;line-height:1.5}
textarea.smn-textarea:not([data-slot]){padding:6px 8px;border-radius:4px;border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-input,transparent);color:var(--ui-text-primary)}
.smn-editor-foot{display:flex;align-items:center;justify-content:space-between;gap:8px}

/* almost there */
.smn-near-tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;scroll-margin-top:16px}
.smn-search{width:220px;max-width:100%}
.smn-filter{display:flex;align-items:center;gap:4px}
.smn-cand{display:flex;flex-direction:column}
.smn-cand-main{display:flex;flex-direction:column;gap:7px;width:100%;padding:12px 16px 4px;border:0;background:transparent;color:inherit;font:inherit;text-align:left;cursor:pointer}
.smn-cand:hover{background:var(--ui-row-hover-background)}
.smn-cand-top{display:flex;align-items:flex-start;gap:10px}
.smn-cand-top .smn-fact{flex:1 1 auto}
.smn-caret{color:var(--ui-text-quaternary);margin-top:3px}
.smn-cand-sub{display:flex;align-items:center;flex-wrap:wrap;gap:6px 8px}
.smn-missing-list{display:flex;flex-wrap:wrap;gap:4px 14px}
.smn-missing{display:inline-flex;align-items:center;gap:5px;font-size:0.71875rem;color:var(--ui-text-secondary)}
.smn-missing>*:first-child{color:var(--ui-orange)}
.smn-missing.is-pass>*:first-child{color:var(--ui-green)}
.smn-cand-actions{padding:2px 16px 10px 16px}
.smn-cand-actions.smn-row-actions{margin-left:-6px}
.smn-cand-detail{padding:6px 16px 6px}
.smn-cand-cols{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
.smn-cand-cols>.smn-grow{display:flex;flex-direction:column;gap:10px;flex-basis:260px}
.smn-cand-why{flex:0 1 240px;min-width:200px;padding:10px 12px;border-radius:6px;background:var(--ui-bg-quaternary)}
.smn-quotes{display:flex;flex-direction:column;gap:6px}
.smn-quote{margin:0;padding:2px 0 2px 10px;border-left:1px solid var(--ui-stroke-secondary);font-size:0.75rem;line-height:1.5;color:var(--ui-text-secondary);font-style:italic}
.smn-quote .smn-meta{font-style:normal}
.smn-tabs{display:inline-flex;gap:2px}
.smn-tab{border:0;background:transparent;padding:2px 8px;font:inherit;font-size:0.6875rem;color:var(--ui-text-secondary);border-radius:3px;cursor:pointer}
.smn-tab[aria-pressed=true]{background:var(--ui-bg-quaternary);color:var(--ui-text-primary)}
.smn-input{height:26px;padding:0 8px;border-radius:3px;border:1px solid var(--ui-stroke-secondary);background:transparent;color:var(--ui-text-primary);font:inherit;font-size:0.75rem}

/* charts */
.smn-chart{position:relative}
.smn-chart-foot{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;font-size:0.6875rem;color:var(--ui-text-tertiary)}
.smn-ring-swatch{display:inline-block;width:8px;height:8px;border-radius:50%;box-shadow:inset 0 0 0 1.2px var(--ui-orange)}
.smn-chart-svg,.smn-constellation,.smn-hist{display:block;width:100%;height:auto;overflow:visible}
.smn-chart-label{font-size:8.5px;fill:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.smn-chart-hit{cursor:pointer;outline:none}
.smn-tip{position:absolute;bottom:calc(100% - 8px);transform:translateX(-50%);display:flex;flex-direction:column;gap:1px;min-width:180px;padding:7px 9px;border-radius:6px;background:var(--ui-bg-elevated,var(--ui-bg-card));box-shadow:0 0 0 1px var(--ui-stroke-tertiary),0 6px 18px -6px color-mix(in srgb,var(--ui-text-primary) 25%,transparent);font-size:0.6875rem;color:var(--ui-text-secondary);pointer-events:none;z-index:5;white-space:nowrap}
.smn-tip strong{color:var(--ui-text-primary);font-weight:600;margin-bottom:2px}
.smn-star{cursor:pointer;outline:none}
.smn-star:focus-visible circle:nth-of-type(2){stroke-width:2}
.smn-star-label{font-size:9.5px;fill:var(--ui-text-secondary)}
.smn-star-label.is-on{fill:var(--ui-text-primary);font-weight:600}

/* diary */
.smn-diary{display:flex;flex-direction:column;gap:22px}
.smn-diary-entry{display:flex;flex-direction:column;gap:6px;max-width:68ch}
.smn-diary-entry.is-fallback{opacity:.6}
.smn-diary-date{margin:0;font-size:0.6875rem;font-weight:600;color:var(--ui-text-tertiary);letter-spacing:.01em}
.smn-diary-text{font-size:0.84375rem;line-height:1.72;color:var(--ui-text-secondary)}
.smn-diary-text p{margin:0 0 10px}
.smn-diary-text p:last-child{margin-bottom:0}

/* calibration */
.smn-details{border-top:1px solid var(--ui-stroke-tertiary);padding-top:14px;margin-top:-12px}
.smn-summary{display:flex;align-items:center;gap:6px;cursor:pointer;list-style:none;user-select:none}
.smn-summary::-webkit-details-marker{display:none}
.smn-summary-caret{color:var(--ui-text-tertiary);transition:transform .15s}
.smn-details[open] .smn-summary-caret{transform:rotate(90deg)}
.smn-calib{display:flex;flex-direction:column;gap:12px;padding:10px 0 0 18px}
.smn-gates{display:flex;flex-direction:column;gap:4px}
.smn-gate{display:grid;grid-template-columns:130px 1fr 20px;align-items:center;gap:8px;font-size:0.6875rem;color:var(--ui-text-secondary)}
.smn-gate-bar{height:5px;border-radius:2px;background:color-mix(in srgb,var(--ui-base,var(--ui-text-primary)) 7%,transparent);overflow:hidden}
.smn-gate-bar span{display:block;height:100%;background:color-mix(in srgb,var(--ui-orange) 70%,transparent)}
.smn-gate-n{text-align:right;font-variant-numeric:tabular-nums}

/* principles + steppers */
.smn-thinks{display:flex;flex-direction:column;gap:2px}
.smn-principle{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:10px;align-items:start;padding:8px 0;border-bottom:1px solid var(--ui-stroke-quaternary,var(--ui-stroke-tertiary))}
.smn-principle-icon{color:var(--ui-text-quaternary);margin-top:2px}
.smn-principle.is-on .smn-principle-icon{color:var(--ui-purple)}
.smn-principle-text{display:flex;flex-direction:column;gap:1px;cursor:pointer}
.smn-principle-label{font-size:0.75rem;font-weight:500;color:var(--ui-text-primary)}
.smn-principle:not(.is-on) .smn-principle-label{color:var(--ui-text-secondary)}
.smn-optin{display:inline-block;margin-left:6px;padding:0 4px;border-radius:3px;font-size:0.59375rem;font-weight:500;line-height:14px;vertical-align:1px;color:var(--ui-text-tertiary);box-shadow:inset 0 0 0 1px var(--ui-stroke-secondary)}
.smn-principle>button,.smn-principle>input{margin-top:3px}
.smn-thinks-controls{display:flex;flex-direction:column;gap:8px;padding-top:12px}
.smn-stepper{display:flex;align-items:center;justify-content:space-between;gap:10px}
.smn-stepper-label{font-size:0.75rem;color:var(--ui-text-secondary)}
.smn-stepper-ctl{display:inline-flex;align-items:center;gap:2px}
.smn-stepper-val{min-width:34px;text-align:center;font-size:0.75rem;font-weight:600;font-variant-numeric:tabular-nums}

/* preview dialog */
.smn-dialog{max-width:620px}
.smn-preview{display:flex;flex-direction:column;gap:12px;max-height:60vh;overflow-y:auto;padding-right:4px}
.smn-preview-row{display:flex;flex-direction:column;gap:6px;padding-bottom:12px;border-bottom:1px solid var(--ui-stroke-tertiary)}
.smn-preview-row:last-child{border-bottom:0}
.smn-modal-backdrop{position:fixed;inset:0;display:grid;place-items:center;background:color-mix(in srgb,var(--ui-text-primary) 25%,transparent);z-index:50}
.smn-modal{display:flex;flex-direction:column;gap:12px;width:min(520px,92vw);padding:18px;border-radius:10px;background:var(--ui-bg-elevated,var(--ui-bg-card))}

/* fallbacks */
.smn-skel{border-radius:4px;background:var(--ui-bg-quaternary);animation:smn-pulse 1.6s ease-in-out infinite}
@keyframes smn-pulse{50%{opacity:.5}}
.smn-btn{display:inline-flex;align-items:center;gap:6px;border:0;background:transparent;color:var(--ui-text-secondary);font:inherit;font-size:0.6875rem;padding:2px 8px;border-radius:3px;cursor:pointer}
.smn-btn:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.smn-btn-default{background:var(--ui-accent);color:var(--dt-primary-foreground,white)}

/* surfaces */
.smn-status{display:inline-flex;height:100%;align-items:center;gap:4px;padding:0 6px;border:0;background:transparent;color:var(--ui-accent);font:inherit;font-size:0.6875rem;font-variant-numeric:tabular-nums;cursor:pointer}
.smn-status:hover{background:var(--chrome-action-hover)}
.smn-fed{display:inline-flex;align-items:center;color:color-mix(in srgb,var(--ui-accent) 80%,transparent);margin:0 2px}
.smn-chat-empty{display:flex;justify-content:center;padding:12px}
.smn-chat-empty-line{display:inline-flex;align-items:baseline;gap:6px;max-width:min(560px,90%);border:0;background:transparent;font:inherit;font-size:0.75rem;color:var(--ui-text-secondary);cursor:pointer;padding:4px 8px;border-radius:4px}
.smn-chat-empty-line:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.smn-chat-empty-line>.codicon,.smn-chat-empty-line>i{color:var(--ui-accent);align-self:center}
.smn-chat-empty-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.smn-card{display:flex;flex-direction:column;gap:10px;max-width:560px;margin:6px 0;padding:12px 14px 8px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-bg-card,transparent);font-size:0.8125rem;color:var(--ui-text-primary);user-select:text}
.smn-card-muted{flex-direction:row;align-items:center;gap:8px;padding:8px 12px;color:var(--ui-text-tertiary)}
.smn-card-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.smn-card-body{display:flex;gap:12px;align-items:flex-start}
.smn-card-body .smn-grow{display:flex;flex-direction:column;gap:7px}
.smn-card .smn-signals{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:28px;row-gap:3px}
.smn-card .smn-signal-foot{grid-column:1/-1}
.smn-card .smn-row-actions{border-top:1px solid var(--ui-stroke-quaternary,var(--ui-stroke-tertiary));padding-top:6px;margin-left:-6px}

@media (prefers-reduced-motion: reduce){.smn-page *{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
`

function injectStyles(onDispose) {
  if (typeof document === 'undefined') return
  document.querySelectorAll(`style[data-plugin="${PLUGIN_ID}"]`).forEach(el => el.remove())
  const el = document.createElement('style')
  el.dataset.plugin = PLUGIN_ID
  el.textContent = STYLESHEET
  document.head.appendChild(el)
  onDispose(() => el.remove())
}

// ==== register ==============================================================

export default {
  id: PLUGIN_ID,
  name: 'Somnia',
  description: 'Nightly memory consolidation: what Hermes learned while you slept, why, and how to undo it.',
  defaultEnabled: true,
  register(ctx) {
    const onDispose = typeof ctx.onDispose === 'function' ? fn => ctx.onDispose(fn) : () => {}
    i18n = ctx.i18n && typeof ctx.i18n.register === 'function' && typeof ctx.i18n.t === 'function' ? ctx.i18n : null
    if (i18n) i18n.register(LOCALES)
    storage = ctx.storage || null
    $seenRun.set(stored('seen_run', 0))
    $dismissed.set(stored('dismissed', []))
    injectStyles(onDispose)

    const register = rows => (typeof ctx.registerMany === 'function' ? ctx.registerMany(rows) : rows.map(r => ctx.register(r)))

    // Static contributions (no translated labels).
    const rows = [{ id: 'page', area: ROUTES_AREA || 'routes', data: { path: ROUTE }, render: () => h(Page) }]
    if (STATUSBAR_AREAS && STATUSBAR_AREAS.right && typeof useQuery === 'function')
      rows.push({ id: 'status', area: STATUSBAR_AREAS.right, order: 70, render: () => h(StatusChip) })
    if (TRANSCRIPT_DIRECTIVE_AREA)
      rows.push({ id: 'directive', area: TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'somnia-memory', render: props => h(DirectiveCard, props) } })
    if (SESSION_ROW_AREAS && SESSION_ROW_AREAS.trailing)
      rows.push({ id: 'session-mark', area: SESSION_ROW_AREAS.trailing, data: { render: ({ sessionId }) => h(SessionMark, { sessionId }) } })
    if (CHAT_EMPTY_AREA) rows.push({ id: 'chat-empty', area: CHAT_EMPTY_AREA, data: { render: () => h(ChatEmptyLine) } })
    if (SIDEBAR_NAV_PREFS_AREA)
      rows.push({ id: 'nav-prefs', area: SIDEBAR_NAV_PREFS_AREA, order: 1000, data: { order: ['new-session', 'capabilities', `${PLUGIN_ID}:nav`] } })
    register(rows)

    // Labelled contributions: Desktop reads labels once, so re-register on a language switch.
    const labelled = () => {
      const list = [
        { id: 'nav', area: SIDEBAR_NAV_AREA || 'sidebar.nav', order: 55, data: { path: ROUTE, label: tr('name'), codicon: 'sparkle' } },
        {
          id: 'open',
          area: PALETTE_AREA || 'palette',
          data: { id: 'somnia.open', action: 'somnia.open', label: tr('open.palette'), keywords: ['somnia', 'dream', 'memory', 'remember', 'forget', 'diary'], run: () => go(ROUTE) }
        }
      ]
      if (KEYBINDS_AREA)
        list.push({ id: 'open-key', area: KEYBINDS_AREA, data: { id: 'somnia.open', label: tr('open.keybind'), category: 'view', defaults: ['mod+alt+s'], run: () => go(ROUTE) } })
      return list
    }
    let disposeLabels = register(labelled())
    let disposeSettings = null
    const settingsPage = () => {
      if (typeof ctx.registerSettingsPage !== 'function') return null
      return ctx.registerSettingsPage({ id: 'somnia', title: tr('name'), icon: 'sparkle', order: 10, render: () => h(SettingsLanding) })
    }
    disposeSettings = settingsPage()
    if (i18n && typeof i18n.onLocaleChange === 'function')
      i18n.onLocaleChange(() => {
        if (typeof disposeLabels === 'function') disposeLabels()
        disposeLabels = register(labelled())
        if (typeof disposeSettings === 'function') disposeSettings()
        disposeSettings = settingsPage()
      })

    onDispose(() => {
      STORES.forEach(s => s.reset())
      serverPrinciples = null
      storage = null
      i18n = null
    })
  }
}
