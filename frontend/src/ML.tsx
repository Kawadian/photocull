import { Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { json, message, request, useRemote } from './api'
import type { Asset, Page, Progress, Tag } from './api'

export type Lookup = { title: string; url: string; body: { query?: string; tag?: string } }
type SearchResponse = {
  status: 'pending' | 'running' | 'done' | 'error'
  items: Asset[]
  error: string | null
}

export function usePhotoSearch(base: string, lookup: Lookup | undefined, retry: number) {
  const [result, setResult] = useState<{ lookup: Lookup; retry: number; data?: Page<Asset>; error?: string }>()
  useEffect(() => {
    if (!lookup) return
    const controller = new AbortController()
    let timer: number | undefined
    const { signal } = controller
    const fail = (error: unknown) => {
      if (!signal.aborted) setResult({ lookup, retry, error: message(error) })
    }
    async function search(task: Lookup) {
      try {
        const queued = await request<{ id: string }>(task.url, { ...json('POST', task.body), signal })
        const deadline = Date.now() + 115_000
        let delay = 200
        async function poll() {
          try {
            const status = await request<SearchResponse>(`${base}/search/${queued.id}`, { signal })
            if (signal.aborted) return
            if (status.status === 'done') {
              setResult({ lookup: task, retry, data: { items: status.items, next_cursor: null } })
            } else if (status.status === 'error') {
              throw new Error(status.error ?? 'Photo search failed.')
            } else if (Date.now() >= deadline) {
              throw new Error('The worker is taking too long. Try the search again.')
            } else {
              timer = window.setTimeout(() => { void poll() }, delay)
              delay = Math.min(delay + 100, 700)
            }
          } catch (error) {
            fail(error)
          }
        }
        if (!signal.aborted) await poll()
      } catch (error) {
        fail(error)
      }
    }
    void search(lookup)
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [base, lookup, retry])
  return result?.lookup === lookup && result?.retry === retry ? result : undefined
}

export function PhotoTools({ base, progress, tag, selectTag, onSearch }: {
  base: string
  progress?: Progress
  tag: string
  selectTag: (tag: string) => void
  onSearch: (text: string) => void
}) {
  const [query, setQuery] = useState('')
  const ml = progress?.ml
  const tags = useRemote<{ items: Tag[] }>(ml?.tags_enabled ? `${base}/tags` : null)
  const error = ml?.image_error || ml?.text_error
  const disabled = !ml?.search_enabled || ml.waiting || !!error

  return (
    <div className="photo-tools">
      <form role="search" onSubmit={(event) => {
        event.preventDefault()
        if (query.trim() && !disabled) onSearch(query.trim())
      }}>
        <label className="section-heading" htmlFor="photo-query">Search photos</label>
        <div className="photo-query">
          <input id="photo-query" type="search" placeholder="猫 or a cat" value={query}
            maxLength={256} disabled={disabled} onChange={(event) => setQuery(event.target.value)} />
          <button type="submit" className="icon-button" aria-label="Search photos"
            disabled={disabled || !query.trim()}><Search size={16} /></button>
        </div>
      </form>
      {ml && !ml.enabled && <p className="score-state">Photo search, similarity and tags are disabled. Enable ML on the worker and API.</p>}
      {ml?.waiting && <p className="score-state" role="status">Waiting for the worker to configure embeddings.</p>}
      {ml?.enabled && !ml.search_enabled && <p className="score-state">Text search is disabled.</p>}
      {ml?.tags_enabled && (
        <>
          <label className="section-heading" htmlFor="photo-tag">Photo tag</label>
          <select id="photo-tag" value={tag} disabled={!tags?.data}
            onChange={(event) => selectTag(event.target.value)}>
            <option value="">{tags ? 'All tags' : 'Loading tags…'}</option>
            {tags?.data?.items.map(({ tag, label }) => <option value={tag} key={tag}>{label}</option>)}
          </select>
          {tags?.error && <p className="inline-error" role="alert">{tags.error}</p>}
        </>
      )}
    </div>
  )
}
