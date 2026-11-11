#!/usr/bin/env node
// Drives the harness in the local headless Chromium over CDP (no npm deps;
// Node ≥ 22 for the global WebSocket). Waits for the page to report a result
// instead of relying on --virtual-time-budget (which does not wait for the
// esm.sh module graph), then saves a screenshot and prints the result JSON.
//
//   node dev/harness/capture.mjs --qs "theme=dark&fixture=demo" --out page.png \
//        [--width 1440] [--height 2200] [--wait 2500] [--full] [--clip "#selector"] [--pad 16]
//
// Exit code 1 when the page reported errors or a scripted flow failed.

import { spawn } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith('--') ? [...acc, [a.slice(2), all[i + 1] && !all[i + 1].startsWith('--') ? all[i + 1] : true]] : acc), [])
)
const width = Number(args.width || 1440)
const height = Number(args.height || 2200)
const chrome = process.env.CHROME || '/opt/hermes/tools/chromium-1208/chrome-linux64/chrome'
const profile = mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'smn-cdp-'))
const url = `file://${join(here, 'index.html')}?${args.qs || ''}`

const proc = spawn(chrome, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files',
  `--user-data-dir=${profile}`, '--remote-debugging-port=0', `--window-size=${width},${height}`, 'about:blank'
], { stdio: ['ignore', 'ignore', 'pipe'] })

const sleep = ms => new Promise(r => setTimeout(r, ms))
async function devtoolsPort() {
  for (let i = 0; i < 100; i++) {
    const f = join(profile, 'DevToolsActivePort')
    if (existsSync(f)) return readFileSync(f, 'utf8').split('\n')[0]
    await sleep(100)
  }
  throw new Error('chrome did not start')
}

let ws
let seq = 0
const pending = new Map()
function send(method, params = {}, sessionId) {
  const id = ++seq
  ws.send(JSON.stringify({ id, method, params, sessionId }))
  return new Promise((res, rej) => pending.set(id, { res, rej }))
}

async function main() {
  const port = await devtoolsPort()
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  const page = targets.find(t => t.type === 'page')
  ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise(r => ws.addEventListener('open', r))
  ws.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id)
      pending.delete(msg.id)
      msg.error ? p.rej(new Error(msg.error.message)) : p.res(msg.result)
    }
  })
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: Number(args.scale || 1), mobile: false })
  await send('Page.navigate', { url })
  const evaluate = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value
  let result = null
  const t0 = Date.now()
  while (Date.now() - t0 < 45000) {
    const txt = await evaluate("document.getElementById('harness-result') && document.getElementById('harness-result').textContent")
    if (txt) { result = JSON.parse(txt); break }
    await sleep(250)
  }
  await sleep(Number(args.wait || 600))
  if (args.eval) await evaluate(args.eval)
  if (args.eval) await sleep(500)
  // Fresh copy of errors after the settle wait.
  const errors = await evaluate('window.__errors')
  if (result) result.errors = errors
  if (args.out) {
    let clip
    if (args.clip) {
      const r = await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(args.clip)}); if (!el) return null; el.scrollIntoView({block:'start'}); const b = el.getBoundingClientRect(); return { x: b.left, y: b.top, width: b.width, height: b.height } })()`)
      await sleep(300)
      const r2 = await evaluate(`(() => { const b = document.querySelector(${JSON.stringify(args.clip)}).getBoundingClientRect(); return { x: b.left, y: b.top, width: b.width, height: b.height } })()`)
      const pad = Number(args.pad ?? 16)
      if (r && r2) clip = { x: Math.max(0, r2.x - pad), y: Math.max(0, r2.y - pad), width: r2.width + pad * 2, height: r2.height + pad * 2, scale: 1 }
    }
    const shot = await send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip } : {}), captureBeyondViewport: !!clip })
    writeFileSync(resolve(args.out), Buffer.from(shot.data, 'base64'))
  }
  console.log(JSON.stringify(result ?? { error: 'no result from page' }))
  const bad = !result || (errors && errors.length) || result.ok === false
  return bad ? 1 : 0
}

main()
  .then(code => finish(code))
  .catch(err => { console.error(String(err && err.stack || err)); finish(2) })

function finish(code) {
  try { ws && ws.close() } catch {}
  proc.kill('SIGKILL')
  setTimeout(() => { try { rmSync(profile, { recursive: true, force: true }) } catch {} ; process.exit(code) }, 200)
}
