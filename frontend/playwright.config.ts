import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const url = 'http://127.0.0.1:18787'

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  use: { baseURL: url, trace: 'retain-on-failure' },
  webServer: {
    command: 'uv run --locked --extra ml python frontend/e2e/serve.py',
    cwd: root,
    url,
    timeout: 30_000,
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
  },
})
