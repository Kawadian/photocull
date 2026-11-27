import { useCallback, useEffect, useState } from 'react'
import { message, request } from '../api'
import type { Asset, Page } from '../api'
import { writeFilters } from './filterValues'
import type { FilterValues } from './filterValues'
import type { BrowserLocation } from './location'
import type { BrowserMedia } from './ResponsiveImage'

export type TimelineAsset = Pick<Asset, 'id' | 'kind' | 'day' | 'width' | 'height' | 'duration' |
  'content_hash' | 'preview_status'> & {
  library: string
  name: string
  blurhash?: string | null
  favorite?: boolean
}

export function assetKey(asset: Pick<TimelineAsset, 'library' | 'id'>) {
  return asset.library + ':' + asset.id
}

export function libraryUrl(library: string) {
  return '/api/libraries/' + encodeURIComponent(library)
}

export function browseUrl(endpoint: 'timeline' | 'months' | 'facets', library: string, month = '',
  cursor?: string | null, filters?: FilterValues) {
  const query = new URLSearchParams()
  if (library) query.set('library', library)
  if (month) query.set('month', month)
  if (cursor) query.set('cursor', cursor)
  if (filters) writeFilters(query, filters)
  const serialized = query.toString()
  return '/api/browse/' + endpoint + (serialized ? '?' + serialized : '')
}

type Load = { cursor: string | null }
type Feed = {
  assets: TimelineAsset[]
  next: string | null
  completed: Load | null
  media?: BrowserMedia
  error?: string
}

export function useTimeline({ library, month, filters }: BrowserLocation) {
  const [load, setLoad] = useState<Load>({ cursor: null })
  const [feed, setFeed] = useState<Feed>({ assets: [], next: null, completed: null })
  const busy = feed.completed !== load
  const url = browseUrl('timeline', library, month, load.cursor, filters)

  useEffect(() => {
    const controller = new AbortController()
    request<Page<TimelineAsset, string> & { media?: BrowserMedia }>(url, {
      signal: controller.signal,
    }).then(({ items, next_cursor, media }) => {
      if (controller.signal.aborted) return
      setFeed((previous) => {
        const existing = load.cursor ? previous.assets : []
        const seen = new Set(existing.map(assetKey))
        return {
          assets: [...existing, ...items.filter((item) => !seen.has(assetKey(item)))],
          next: next_cursor,
          completed: load,
          media,
        }
      })
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        setFeed((previous) => ({ ...previous, completed: load, error: message(error) }))
      }
    })
    return () => controller.abort()
  }, [load, url])

  function loadMore() {
    if (busy) return
    if (feed.error) setLoad({ cursor: load.cursor })
    else if (feed.next) setLoad({ cursor: feed.next })
  }

  const reload = useCallback(() => setLoad({ cursor: null }), [])
  function setFavorite(key: string, favorite: boolean) {
    setFeed((previous) => ({
      ...previous,
      assets: previous.assets.map((asset) => assetKey(asset) === key ? { ...asset, favorite } : asset),
    }))
  }

  return { ...feed, busy, loadMore, reload, setFavorite }
}
