import { json, request } from './api'

export type Field = {
  value: unknown
  default: unknown
  running: unknown
  source: string
  restart: 'api-worker' | 'egress-proxy' | 'live'
  pending: boolean
  type: string
  nullable: boolean
  path?: string
  enum?: string[]
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  multipleOf?: number
  maxLength?: number
  pattern?: string
}

export type RestartService = 'worker' | 'egress-proxy' | 'api'

export type RestartInfo = {
  services: RestartService[]
  pending_fields: string[]
  automatic: boolean
  busy: boolean
  result: {
    request: { id: string; revision: string; requested_at: number; services: RestartService[] }
    status: 'queued' | 'running' | 'succeeded' | 'failed' | 'expired'
    restarted: RestartService[]
    finished_at?: number
  } | null
}

export type Settings = {
  restart: RestartInfo
  fields: Record<string, Field>
  revision: string
  history: string[]
  locked_libraries: Record<string, string>
  api_key_configured: boolean
  network: { worker: string; proxy: string; proxy_reads_saved_settings: boolean }
  warnings: string[]
}

export type Changes = Record<string, unknown>
export type Diff = {
  changes: { key: string; before: unknown; after: unknown }[]
  warnings: string[]
}

const api = '/api/settings'

export function settingsRequest<T>(route = '', data?: object, method: 'POST' | 'PUT' = 'POST') {
  return request<T>(api + route, data ? json(method, data) : undefined)
}

export function formatValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return '未設定'
  if (value === true) return '有効'
  if (value === false) return '無効'
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}
