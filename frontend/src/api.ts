import { useEffect, useState } from 'react'

export type Library = { name: string }
export type Page<T, Cursor = number> = { items: T[]; next_cursor: Cursor | null }
export type Group = { name: string; count: number }
export type GroupBy = 'folder' | 'day'
export type StageStatus = 'pending' | 'running' | 'done' | 'error' | 'skipped'
export type Rating = { rating: number; rejected: boolean; revision: number }
export type Asset = Rating & {
  id: number
  path: string
  folder: string
  kind: 'image' | 'raw' | 'heif' | 'video'
  day: string
  content_hash: string
  captured_at: string | null
  make: string | null
  model: string | null
  width: number | null
  height: number | null
  duration: number | null
  preview_status: StageStatus | null
  preview_error: string | null
}
export type Progress = {
  catalog_revision: number
  scan: { id: number; status: StageStatus; error: string | null } | null
  stages: Record<'exif' | 'preview', Partial<Record<StageStatus, number>>>
  worker: { heartbeat: number; pid: number } | null
}
export type XmpPlan = {
  sidecar: string
  rating: number
  action: 'create' | 'update' | 'unchanged'
  confirmation: string
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    const payload: { detail?: unknown } = await response.json().catch(() => ({}))
    const detail = typeof payload.detail === 'string' ? payload.detail : response.statusText
    throw new ApiError(response.status, detail || `Request failed (${response.status})`)
  }
  return (await response.json()) as T
}

export function json(method: 'POST' | 'PATCH', body: object): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export function message(error: unknown): string {
  return error instanceof Error ? error.message : 'The request failed.'
}

export function useRemote<T>(url: string, version = 0) {
  const [result, setResult] = useState<{ url: string; data?: T; error?: string }>()
  useEffect(() => {
    const controller = new AbortController()
    request<T>(url, { signal: controller.signal })
      .then((data) => setResult({ url, data }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setResult((previous) => ({
            url,
            data: previous?.url === url ? previous.data : undefined,
            error: message(error),
          }))
        }
      })
    return () => controller.abort()
  }, [url, version])
  return result?.url === url ? result : undefined
}

export function useProgress(base: string) {
  const [progress, setProgress] = useState<Progress>()
  const [connected, setConnected] = useState(false)
  useEffect(() => {
    const source = new EventSource(`${base}/events`)
    source.onopen = () => setConnected(true)
    source.onmessage = (event: MessageEvent<string>) => {
      setProgress(JSON.parse(event.data) as Progress)
    }
    source.onerror = () => setConnected(false)
    return () => source.close()
  }, [base])
  return { progress, connected }
}

export function assetName(asset: Asset): string {
  return asset.path.slice(asset.path.lastIndexOf('/') + 1)
}

export function mediaUrl(base: string, asset: Asset, variant: 'thumb' | 'preview' | 'detail') {
  return `${base}/assets/${asset.id}/media/${variant}?v=${asset.content_hash}`
}
