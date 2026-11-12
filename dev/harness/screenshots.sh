#!/usr/bin/env bash
# Regenerates the README screenshots in every documented language.
# Usage: bash dev/harness/screenshots.sh   (needs Chromium; see dev/harness/README.md)
set -u
cd "$(dirname "$0")"
OUT=../../docs/screenshots
PREVIEW_CLICK='new Promise(r => { const b = [...document.querySelectorAll("button")].find(x => /^(Preview|Prévia)$/.test(x.textContent.trim())); b && b.click(); setTimeout(r, 7000) })'
OPTIMIZE_CLICK='new Promise(r => { const b = [...document.querySelectorAll("button")].find(x => /^(Optimize|Otimizar)$/.test(x.textContent.trim())); b && b.click(); setTimeout(r, 4500) })'
LIMIT_CLICK='new Promise(r => { const b = [...document.querySelectorAll("button")].find(x => /^(Limit|Limite)$/.test(x.textContent.trim())); b && b.click(); setTimeout(() => { const c = document.querySelectorAll(".smn-lchip")[2]; c && c.click(); setTimeout(r, 400) }, 400) })'
fail=0
shot() { # dir name qs [extra args...]
  local dir=$1 name=$2 qs=$3; shift 3
  mkdir -p "$OUT/$dir"
  if node capture.mjs --qs "$qs" --out "$OUT/$dir/$name.png" "$@" > ${TMPDIR:-/tmp}/smn-shot.json; then
    echo "ok   $dir/$name"
  else
    echo "FAIL $dir/$name $(cat ${TMPDIR:-/tmp}/smn-shot.json)"; fail=1
  fi
}
for lang in en pt-BR; do
  l=$(echo "$lang" | tr 'A-Z' 'a-z')
  shot "$lang" page        "theme=dark&fixture=demo&lang=$l" --height 1180
  shot "$lang" page-full   "theme=dark&fixture=demo&lang=$l" --height 3400
  shot "$lang" page-light  "theme=light&fixture=demo&lang=$l" --height 1180
  shot "$lang" memory      "theme=dark&fixture=demo&lang=$l&script=memory" --clip "#smn-space"
  shot "$lang" almost-there "theme=dark&fixture=demo&lang=$l" --clip "#smn-near" --height 3400
  shot "$lang" sleep       "theme=dark&fixture=demo&lang=$l" --clip "#smn-sleep" --height 3400
  shot "$lang" principles  "theme=dark&fixture=demo&lang=$l" --clip "#smn-thinks" --height 3400
  shot "$lang" diary       "theme=dark&fixture=demo&lang=$l" --clip "#smn-diary" --height 3400
  shot "$lang" chat-card   "theme=dark&fixture=demo&lang=$l&view=chat" --clip ".hx-msgs" --height 1100
  shot "$lang" settings    "theme=dark&fixture=demo&lang=$l&view=settings" --height 900
  shot "$lang" preview     "theme=dark&fixture=demo&lang=$l" --height 1000 --eval "$PREVIEW_CLICK"
  shot "$lang" optimize    "theme=dark&fixture=demo&lang=$l" --height 1000 --eval "$OPTIMIZE_CLICK"
  shot "$lang" limit       "theme=dark&fixture=demo&lang=$l" --clip "#smn-space" --eval "$LIMIT_CLICK"
  shot "$lang" empty       "theme=dark&fixture=empty&lang=$l" --height 1000
  shot "$lang" alarm       "theme=dark&fixture=demo&lang=$l&alarm=noop" --height 700
done
exit $fail
