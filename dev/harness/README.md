# Somnia desktop harness

The real Electron app is not needed: `index.html` loads `../../desktop/plugin.js` unmodified, with
`@hermes/plugin-sdk` mapped (import map) to `sdk-shim.js` — the exports the plugin uses, with the real prop API and
the real Tailwind class strings from `apps/desktop/src/components/ui/*`. React 19 / React Query come from esm.sh,
Tailwind v4 from the browser build, codicons 0.0.45 from jsDelivr, so it needs internet.

- `theme.css` — the app's `:root` / `:root.dark` variable blocks + the Nous skin seeds. Regenerate:
  `node dev/harness/build-theme.mjs [/path/to/apps/desktop/src/styles.css]`.
- `fixtures/demo.json`, `fixtures/empty.json` — fictional data (Maya, an indie dev) matching `docs/rpc.md`.
  Regenerate: `node dev/harness/fixtures/make-fixtures.mjs`.
- `mock-host.js` — the Python half in memory: serves every read RPC, mutates the fixture on writes, runs jobs
  (~2.6 s), `fixture=disabled` answers `-32601`. Calls are logged in `window.__rpcLog`.

Query params: `theme=dark|light`, `fixture=demo|empty|disabled`, `view=page|chat|newchat`, `lang=en|pt-br`,
`alarm=noop`, `script=<flow>`, `realtime=1` (otherwise the clock is pinned to the fixture's `now`).
Errors (console.error, uncaught, unhandled rejections, boundary catches) go to `window.__errors` and a red overlay.

Capture (headless Chromium over CDP; waits for the page instead of a fixed time budget):

```bash
node dev/harness/capture.mjs --qs "theme=dark&fixture=demo" --out /tmp/page.png --height 2300
node dev/harness/capture.mjs --qs "theme=dark&script=forget" --wait 200      # prints {ok, errors, rpc}
node dev/harness/capture.mjs --qs "theme=dark&view=chat" --out card.png --clip ".hx-msgs"
```

Exit code is non-zero when the page logged errors or the flow failed. Flows: forget keep edit memory memoryForget
preview dream undo promote pin expand search tabs ask session night theme principles calibration locale disabled
(fixture=disabled) directive directiveOpen (view=chat). Opening `index.html` in a desktop browser also works when
launched with file access allowed for modules (e.g. serve the repo with `python3 -m http.server` and open
`/dev/harness/index.html`).
