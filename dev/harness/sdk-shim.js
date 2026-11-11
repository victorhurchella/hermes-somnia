// Harness stand-in for '@hermes/plugin-sdk'.
//
// Implements exactly the exports desktop/plugin.js reaches for, with the same
// prop API as the real SDK (apps/desktop/src/sdk/index.ts). Components reuse the
// real Tailwind class strings from apps/desktop/src/components/ui/* so they look
// like the app; behaviour is simplified (no Radix), never invented.
//
// The host (`host.request`, navigation, composer, toasts) is wired by the frame
// in index.html through `__setHarnessHost()`.

import { QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createElement as h, Fragment, cloneElement, isValidElement, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

export { useMutation, useQuery, useQueryClient }

export const queryClient = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false } } })

// ---------------------------------------------------------------- utilities
export const cn = (...parts) => parts.flat(Infinity).filter(Boolean).join(' ')

export function atom(initial) {
  let value = initial
  const subs = new Set()
  return {
    get: () => value,
    set: next => {
      value = next
      subs.forEach(fn => fn(value))
    },
    subscribe: fn => {
      subs.add(fn)
      return () => subs.delete(fn)
    }
  }
}

export function useValue(store) {
  return useSyncExternalStore(cb => store.subscribe(cb), store.get, store.get)
}

export function relativeTime(targetMs, nowMs = Date.now(), locale) {
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  const diff = targetMs - nowMs
  const abs = Math.abs(diff)
  const s = diff < 0 ? -1 : 1
  if (abs < 60e3) return rtf.format(s * Math.round(abs / 1e3), 'second')
  if (abs < 3600e3) return rtf.format(s * Math.round(abs / 60e3), 'minute')
  if (abs < 86400e3) return rtf.format(s * Math.round(abs / 3600e3), 'hour')
  return rtf.format(s * Math.round(abs / 86400e3), 'day')
}

export function pluginSettingsHref(plugin, page) {
  return `/settings/plugins/${plugin}${page ? `/${page}` : ''}`
}

// ---------------------------------------------------------------- areas
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const SIDEBAR_NAV_PREFS_AREA = 'sidebarNav.prefs'
export const PALETTE_AREA = 'palette'
export const KEYBINDS_AREA = 'keybinds'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directives'
export const SESSION_ROW_AREAS = { leading: 'sessionRow.leading', trailing: 'sessionRow.trailing' }
export const CHAT_EMPTY_AREA = 'chat.empty'

// ---------------------------------------------------------------- i18n
// The frame owns the active locale and the per-plugin bundles.
export const $locale = atom('en')
export const pluginBundles = new Map()

function resolve(table, key) {
  return String(key).split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), table)
}

export function translatePlugin(pluginId, locale, key, args) {
  const bundles = pluginBundles.get(pluginId) || {}
  for (const l of [locale, 'en']) {
    const v = resolve(bundles[l], key)
    if (typeof v === 'function') return v(...args)
    if (typeof v === 'string') return v
  }
  return key
}

export function usePluginI18n(pluginId) {
  const locale = useValue($locale)
  return (key, ...args) => translatePlugin(pluginId, locale, key, args)
}

// ---------------------------------------------------------------- host
const gateway = atom('open')
let impl = {}
export function __setHarnessHost(next) {
  impl = next
  if (next.gatewayState) gateway.set(next.gatewayState)
}

export const host = {
  state: { gateway },
  request: (method, params, timeoutMs) => impl.request(method, params, timeoutMs),
  navigate: path => impl.navigate(path),
  openSession: async (id, options) => impl.openSession(id, options),
  newChat: () => impl.newChat(),
  composer: {
    setDraft: async (sessionId, text) => impl.setDraft(sessionId, text),
    focus: sessionId => impl.focusComposer && impl.focusComposer(sessionId)
  },
  notify: input => impl.notify(input),
  notifyError: (error, fallback) => impl.notify({ kind: 'error', message: (error && (error.data?.detail || error.message)) || fallback })
}

// ---------------------------------------------------------------- Codicon
export function Codicon({ className, name, size, spinning, style, ...props }) {
  return h('i', {
    'aria-hidden': 'true',
    className: cn('codicon', `codicon-${name}`, spinning && 'codicon-modifier-spin', className),
    style: { ...(size != null && size !== '' ? { fontSize: size } : {}), ...style },
    ...props
  })
}

// ---------------------------------------------------------------- Button (classes from components/ui/button.tsx)
const BTN_BASE =
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 rounded-[2.5px] text-xs leading-4 font-medium whitespace-nowrap shadow-none transition-all duration-100 focus-visible:outline-hidden focus-visible:border-ring focus-visible:ring-[0.1875rem] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-default disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0"
const BTN_VARIANT = {
  default: 'bg-primary text-primary-foreground hover:bg-primary/90',
  destructive: 'bg-destructive text-white hover:bg-destructive/90 dark:bg-destructive/60',
  outline: 'bg-transparent text-(--ui-text-primary) shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--ui-stroke-secondary)_50%,transparent)] hover:bg-(--chrome-action-hover)',
  secondary: 'bg-(--ui-bg-quaternary) text-(--ui-text-primary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
  ghost: 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
  text: 'text-muted-foreground underline-offset-4 hover:text-foreground hover:underline',
  link: 'text-primary underline-offset-4 hover:underline'
}
const BTN_SIZE = {
  default: 'px-3 py-1.5',
  xs: 'gap-1 px-2 py-0.5 text-[0.6875rem] leading-4',
  sm: 'px-2.5 py-1',
  lg: 'px-5 py-2 text-sm leading-5',
  'icon-xs': 'size-6 rounded-[4px]',
  'icon-sm': 'size-8 rounded-[4px]'
}

export function Button({ className, variant = 'default', size = 'default', asChild, loading = false, children, disabled, ...props }) {
  // Icon-only buttons in the app use the icon sizes; the shim mirrors has-[>svg] padding loosely.
  const kids = Array.isArray(children) ? children.filter(Boolean) : children ? [children] : []
  const iconOnly = kids.length === 1 && isValidElement(kids[0]) && kids[0].type === Codicon
  return h(
    'button',
    {
      'aria-busy': loading || undefined,
      className: cn(BTN_BASE, BTN_VARIANT[variant] || BTN_VARIANT.default, BTN_SIZE[size] || BTN_SIZE.default, iconOnly && 'px-1.5', loading && 'relative', className),
      'data-slot': 'button',
      'data-variant': variant,
      'data-size': size,
      disabled: disabled || loading,
      ...props
    },
    loading
      ? [
          h('span', { key: 'g', 'aria-hidden': true, className: 'invisible inline-flex items-center gap-[inherit]' }, ...kids),
          h('span', { key: 's', 'aria-hidden': true, className: 'absolute inset-0 grid place-items-center' }, h(Codicon, { name: 'loading', spinning: true, size: '0.8rem' }))
        ]
      : kids
  )
}

// ---------------------------------------------------------------- Badge
export function Badge({ className, variant = 'default', ...props }) {
  return h('span', { className: cn('inline-flex w-fit items-center gap-1 rounded-[3px] px-1.5 py-0.5 text-[0.65rem] font-medium', variant === 'muted' ? 'bg-muted text-muted-foreground' : 'bg-primary/10 text-primary', className), ...props })
}

// ---------------------------------------------------------------- Skeleton
export function Skeleton({ className, ...props }) {
  return h('div', { className: cn('animate-pulse rounded-md bg-accent', className), 'data-slot': 'skeleton', ...props })
}

// ---------------------------------------------------------------- Textarea (control.ts classes)
export function Textarea({ className, size, ...props }) {
  return h('textarea', {
    autoCapitalize: 'off',
    autoComplete: 'off',
    spellCheck: false,
    'data-slot': 'textarea',
    className: cn('desktop-input-chrome w-full min-w-0 rounded-[2.5px] border border-(--ui-stroke-secondary) bg-(--ui-bg-input) px-2.5 py-1.5 text-xs leading-4 text-foreground outline-none placeholder:text-muted-foreground focus:border-ring', className),
    ...props
  })
}

// ---------------------------------------------------------------- Switch (switch.tsx classes)
export function Switch({ checked, onCheckedChange, disabled, size = 'default', className, id, ...props }) {
  const track = size === 'xs' ? 'h-4 w-7' : 'h-5 w-9'
  const thumb = size === 'xs' ? cn('size-3', checked && 'translate-x-3.5') : cn('size-4', checked && 'translate-x-4')
  return h(
    'button',
    {
      id,
      type: 'button',
      role: 'switch',
      'aria-checked': !!checked,
      'data-state': checked ? 'checked' : 'unchecked',
      'data-slot': 'switch',
      disabled,
      onClick: () => onCheckedChange && onCheckedChange(!checked),
      className: cn(
        'peer inline-flex shrink-0 items-center rounded-full border border-[color-mix(in_srgb,var(--dt-foreground)_18%,transparent)] bg-[color-mix(in_srgb,var(--dt-background)_58%,var(--dt-input))] shadow-[inset_0_0_0_0.0625rem_color-mix(in_srgb,var(--dt-foreground)_8%,transparent)] transition-colors disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:border-transparent data-[state=checked]:bg-primary',
        track,
        className
      ),
      ...props
    },
    h('span', { 'data-state': checked ? 'checked' : 'unchecked', className: cn('pointer-events-none block rounded-full bg-foreground ring-0 transition-transform data-[state=checked]:bg-background', thumb) })
  )
}

// ---------------------------------------------------------------- SegmentedControl (segmented-control.tsx)
export function SegmentedControl({ className, disabled = false, onChange, options, sizing = 'equal', value }) {
  return h(
    'div',
    { className: cn('inline-grid w-fit grid-flow-col gap-0.5 rounded-[5px] bg-(--ui-bg-tertiary) p-0.5', sizing === 'equal' ? 'auto-cols-fr' : 'auto-cols-max', disabled && 'opacity-50', className) },
    options.map(({ id, label }) =>
      h(
        'button',
        {
          key: id,
          type: 'button',
          'aria-pressed': value === id,
          disabled,
          onClick: () => onChange(id),
          className: cn(
            'flex items-center justify-center gap-1 rounded-[3px] px-2.5 py-0.5 text-[0.6875rem] font-medium transition-colors disabled:cursor-default',
            value === id ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
          )
        },
        label
      )
    )
  )
}

// ---------------------------------------------------------------- SearchField (search-field.tsx)
export function SearchField({ placeholder, value, onChange, onClear, containerClassName, inputClassName, loading, variant = 'underline' }) {
  const boxed = variant === 'box'
  return h(
    'div',
    {
      className: cn(
        'inline-flex min-w-0 max-w-full items-center gap-1.5 transition-[color,border-color,opacity]',
        boxed
          ? 'w-full gap-2 rounded-full border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) px-3.5 py-2 hover:border-(--ui-stroke-secondary) has-[>input:focus]:border-ring'
          : cn('border-b border-transparent px-0.5 has-[>input:focus]:border-ring', !value && 'opacity-30 focus-within:opacity-100'),
        containerClassName
      )
    },
    h(Codicon, { name: 'search', size: boxed ? '1rem' : '0.875rem', className: 'pointer-events-none shrink-0 text-muted-foreground/70' }),
    h('input', {
      'aria-label': placeholder,
      className: cn('h-7 min-w-0 max-w-full flex-1 bg-transparent text-xs text-foreground [field-sizing:content] placeholder:text-muted-foreground focus:outline-hidden', boxed && 'h-auto w-full flex-1 text-sm leading-5 [field-sizing:initial]', inputClassName),
      onChange: e => onChange(e.target.value),
      placeholder,
      type: 'text',
      value
    }),
    loading
      ? h(Codicon, { name: 'loading', spinning: true, size: '0.875rem', className: 'text-muted-foreground/70' })
      : value
        ? h(Button, { 'aria-label': 'Clear', size: 'icon-xs', variant: 'ghost', className: 'shrink-0 text-muted-foreground/85', onClick: () => (onClear ? onClear() : onChange('')) }, h(Codicon, { name: 'close', size: '0.875rem' }))
        : null
  )
}

// ---------------------------------------------------------------- Tip (tooltip on hover/focus, portalled)
export function Tip({ label, children, side = 'top' }) {
  const [rect, setRect] = useState(null)
  if (!label) return children
  if (!isValidElement(children)) return children
  const show = e => setRect(e.currentTarget.getBoundingClientRect())
  const hide = () => setRect(null)
  const child = cloneElement(children, {
    onMouseEnter: e => { children.props.onMouseEnter && children.props.onMouseEnter(e); show(e) },
    onMouseLeave: e => { children.props.onMouseLeave && children.props.onMouseLeave(e); hide() },
    onFocus: e => { children.props.onFocus && children.props.onFocus(e); show(e) },
    onBlur: e => { children.props.onBlur && children.props.onBlur(e); hide() },
    'data-tip': typeof label === 'string' ? label : undefined
  })
  const below = side === 'bottom' || (rect && rect.top < 60)
  return h(
    Fragment,
    null,
    child,
    rect
      ? createPortal(
          h('div', {
            role: 'tooltip',
            className: 'pointer-events-none fixed z-[200] max-w-72 rounded-md bg-popover px-2 py-1 text-[0.6875rem] leading-snug text-popover-foreground shadow-md',
            style: { left: Math.max(8, Math.min(window.innerWidth - 296, rect.left + rect.width / 2 - 144)), top: below ? rect.bottom + 6 : undefined, bottom: below ? undefined : window.innerHeight - rect.top + 6, width: 288, textAlign: 'center' }
          }, h('span', { className: 'inline-block text-left' }, label)),
          document.body
        )
      : null
  )
}

// ---------------------------------------------------------------- Dialog (dialog.tsx shell classes)
export function Dialog({ open, onOpenChange, children }) {
  useEffect(() => {
    if (!open) return undefined
    const onKey = e => e.key === 'Escape' && onOpenChange && onOpenChange(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onOpenChange])
  if (!open) return null
  return createPortal(
    h(
      DialogCtx,
      { onOpenChange },
      h('div', { 'data-slot': 'dialog-overlay', className: 'fixed inset-0 z-[129] bg-black/22 backdrop-blur-[0.125rem]', onClick: () => onOpenChange && onOpenChange(false) }),
      children
    ),
    document.body
  )
}
let currentDialogClose = null
function DialogCtx({ onOpenChange, children }) {
  currentDialogClose = () => onOpenChange && onOpenChange(false)
  return h(Fragment, null, children)
}

export function DialogContent({ className, children, onKeyDown, onOpenAutoFocus, showCloseButton = true }) {
  const ref = useRef(null)
  const close = currentDialogClose
  useEffect(() => {
    if (onOpenAutoFocus) {
      const ev = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true } }
      onOpenAutoFocus(ev)
    }
  }, [])
  return h(
    'div',
    {
      ref,
      role: 'dialog',
      'aria-modal': true,
      'data-slot': 'dialog-content',
      onKeyDown,
      className: cn('fixed left-1/2 top-1/2 z-[130] pointer-events-auto flex max-h-[85vh] -translate-x-1/2 -translate-y-1/2 flex-col rounded-xl border border-(--stroke-nous) bg-(--ui-chat-bubble-background) text-foreground shadow-nous w-full max-w-lg', className)
    },
    h('div', { className: 'grid min-h-0 grid-cols-[minmax(0,1fr)] gap-3 overflow-y-auto rounded-[inherit] p-4' }, children),
    showCloseButton
      ? h(Button, { 'aria-label': 'Close', size: 'icon-xs', variant: 'ghost', className: 'absolute right-2.5 top-2.5 z-20 text-(--ui-text-tertiary)', onClick: () => close && close() }, h(Codicon, { name: 'close', size: '1rem' }))
      : null
  )
}

export const DialogHeader = ({ className, ...p }) => h('div', { className: cn('flex flex-col gap-1 text-center sm:text-left', className), 'data-slot': 'dialog-header', ...p })
export const DialogFooter = ({ className, ...p }) => h('div', { className: cn('flex flex-col-reverse gap-2 sm:flex-row sm:justify-end', className), 'data-slot': 'dialog-footer', ...p })
export const DialogTitle = ({ className, ...p }) => h('h2', { className: cn('text-[0.9375rem] font-semibold tracking-tight text-foreground', className), 'data-slot': 'dialog-title', ...p })
export const DialogDescription = ({ className, ...p }) => h('p', { className: cn('text-xs text-muted-foreground', className), 'data-slot': 'dialog-description', ...p })

// ---------------------------------------------------------------- ConfirmDialog (confirm-dialog.tsx behaviour)
export function ConfirmDialog({ open, onClose, onConfirm, title, description, children, confirmLabel = 'Confirm', busyLabel = 'Working…', doneLabel = 'Done', cancelLabel = 'Cancel', destructive = false }) {
  const [status, setStatus] = useState('idle')
  const [error, setError] = useState(null)
  const confirmRef = useRef(null)
  const busy = status !== 'idle'
  useEffect(() => {
    if (open) {
      setStatus('idle')
      setError(null)
    }
  }, [open])
  async function run() {
    if (busy) return
    setStatus('saving')
    setError(null)
    try {
      await onConfirm()
      setStatus('done')
      setTimeout(onClose, 600)
    } catch (err) {
      setStatus('idle')
      setError(err && err.message ? err.message : 'Something went wrong')
    }
  }
  return h(
    Dialog,
    { open, onOpenChange: v => !v && !busy && onClose() },
    h(
      DialogContent,
      { className: 'max-w-md', onOpenAutoFocus: e => { e.preventDefault(); setTimeout(() => confirmRef.current && confirmRef.current.focus(), 0) } },
      h(DialogHeader, null, h(DialogTitle, null, title), description ? h(DialogDescription, { className: 'whitespace-pre-line' }, description) : null),
      children,
      error ? h('div', { className: 'flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive' }, h('span', null, error)) : null,
      h(
        DialogFooter,
        null,
        h(Button, { disabled: busy, onClick: onClose, type: 'button', variant: 'ghost', 'data-confirm': 'cancel' }, cancelLabel),
        h(Button, { disabled: busy, onClick: () => void run(), ref: confirmRef, variant: destructive ? 'destructive' : 'default', 'data-confirm': 'ok' }, status === 'saving' ? busyLabel : status === 'done' ? doneLabel : confirmLabel)
      )
    )
  )
}

// ---------------------------------------------------------------- Streamdown (markdown-lite)
export function Streamdown({ children }) {
  const text = String(children || '')
  return h('div', { className: 'streamdown' }, text.split(/\n{2,}/).map((p, i) => h('p', { key: i }, p)))
}
