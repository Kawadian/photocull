import console from 'node:console'
import { readFile } from 'node:fs/promises'
import process from 'node:process'
import { URL } from 'node:url'
import { gzipSync } from 'node:zlib'

const dist = new URL('../dist/', import.meta.url)
const manifest = JSON.parse(await readFile(new URL('.vite/manifest.json', dist), 'utf8'))
const visited = new Set()
let bytes = 0

async function measure(key) {
  if (visited.has(key)) return
  visited.add(key)
  const item = manifest[key]
  if (item.file.endsWith('.js')) bytes += gzipSync(await readFile(new URL(item.file, dist))).length
  for (const dependency of item.imports ?? []) await measure(dependency)
}

await measure('index.html')
await measure('src/browser/Browser.tsx')
const limit = 200 * 1024
console.log(`Initial browser JavaScript: ${(bytes / 1024).toFixed(1)} KiB gzipped (budget ${limit / 1024} KiB)`)
if (bytes > limit) process.exitCode = 1
