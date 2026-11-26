import { Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { Ref } from 'react'
import { json, message, request, useRemote } from './api'
import type { Asset, Page, Progress, Tag } from './api'

export type Lookup = { title: string; url: string; body: { query?: string; tag?: string }; source?: Asset }
type SearchResponse = {
  status: 'pending' | 'running' | 'done' | 'error'
  items: Asset[]
  error: string | null
}
type SearchResult = { lookup: Lookup; retry: number; data?: Page<Asset>; error?: string }

export function usePhotoSearch(base: string, lookup: Lookup | undefined, retry: number) {
  const [result, setResult] = useState<SearchResult>()
  useEffect(() => {
    if (!lookup) return
    const task = lookup
    const controller = new AbortController()
    const { signal } = controller
    let pending: string | undefined
    let pollTimer: number | undefined
    let active = true

    function cancel() {
      window.clearTimeout(pollTimer)
      if (pending) {
        void fetch(pending, { method: 'DELETE', keepalive: true }).catch(() => {})
        pending = undefined
      }
    }

    const deadline = window.setTimeout(() => {
      controller.abort()
      cancel()
      setResult({ lookup: task, retry, error: 'Photo search timed out. Try again shortly.' })
    }, 12_000)

    function finish(value: Omit<SearchResult, 'lookup' | 'retry'>) {
      if (!active || signal.aborted) return
      window.clearTimeout(deadline)
      setResult({ ...value, lookup: task, retry })
    }

    async function poll(url: string, delay: number) {
      try {
        const response = await request<SearchResponse>(url, { signal })
        if (signal.aborted) return
        if (response.status === 'pending' || response.status === 'running') {
          pollTimer = window.setTimeout(() => { void poll(url, Math.min(delay + 50, 300)) }, delay)
          return
        }
        pending = undefined
        if (response.status === 'done') {
          finish({ data: { items: response.items, next_cursor: null } })
        } else {
          finish({ error: response.error ?? 'Photo search failed.' })
        }
      } catch (error) {
        if (!signal.aborted) {
          cancel()
          finish({ error: message(error) })
        }
      }
    }

    request<{ id: string }>(task.url, { ...json('POST', task.body), signal })
      .then(({ id }) => {
        pending = `${base}/search/${id}`
        if (signal.aborted) cancel()
        else void poll(pending, 100)
      })
      .catch((error: unknown) => finish({ error: message(error) }))

    return () => {
      active = false
      controller.abort()
      window.clearTimeout(deadline)
      cancel()
    }
  }, [base, lookup, retry])
  return result?.lookup === lookup && result?.retry === retry ? result : undefined
}

export function PhotoTools({ base, progress, tag, selectTag, onSearch, inputRef, onEscape }: {
  base: string
  progress?: Progress
  tag: string
  selectTag: (tag: string) => void
  onSearch: (text: string) => void
  inputRef: Ref<HTMLInputElement>
  onEscape: () => boolean
}) {
  const [query, setQuery] = useState('')
  const [tagRetry, setTagRetry] = useState(0)
  const ml = progress?.ml
  const tags = useRemote<{ items: Tag[] }>(ml?.tags_enabled ? `${base}/tags` : null, tagRetry)
  const error = ml?.text_error || (!progress?.stages.embedding?.done && ml?.image_error)
  const offline = ml?.enabled && !progress?.worker_online
  const disabled = !ml?.search_enabled || ml.waiting || !!offline || !!error
  const status = !ml ? 'Checking search availability…'
    : !ml.enabled ? 'Photo search, similarity and tags are disabled.'
    : offline ? 'The indexing worker is offline. Start it to search.'
    : ml.waiting ? 'Waiting for the worker to configure embeddings.'
    : !ml.search_enabled ? 'Text search is disabled.' : error || ''

  return (
    <div className="photo-tools">
      <form role="search" aria-label="Photo search" onSubmit={(event) => {
        event.preventDefault()
        if (query.trim() && !disabled) onSearch(query.trim())
      }}>
        <label className="section-heading" htmlFor="photo-query">Search photos <kbd aria-hidden="true">/</kbd></label>
        <div className="photo-query">
          <input id="photo-query" type="search" placeholder="猫 or a cat" value={query}
            ref={inputRef} aria-keyshortcuts="/" aria-describedby={status ? 'photo-search-status' : undefined}
            maxLength={256} disabled={disabled} onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Escape' || event.nativeEvent.isComposing) return
              event.preventDefault()
              if (!onEscape()) setQuery('')
            }} />
          <button type="submit" className="icon-button" aria-label="Search photos"
            disabled={disabled || !query.trim()}><Search size={16} /></button>
        </div>
        {status && <p id="photo-search-status" className={error && !offline ? 'inline-error' : 'score-state'}
          role="status">{status}</p>}
      </form>
      {ml?.tags_enabled && (
        <div className="tag-filter">
          <label className="section-heading" htmlFor="photo-tag">Photo tag</label>
          <select id="photo-tag" value={tag} disabled={!tags?.data}
            onChange={(event) => selectTag(event.target.value)}>
            <option value="">{tags ? 'All tags' : 'Loading tags…'}</option>
            {tags?.data?.items.map(({ tag, label }) => <option value={tag} key={tag}>{label}</option>)}
          </select>
          {tags?.error && <>
            <p className="inline-error" role="alert">{tags.error}</p>
            <button className="button quiet" onClick={() => setTagRetry((value) => value + 1)}>Retry tags</button>
          </>}
        </div>
      )}
    </div>
  )
}
