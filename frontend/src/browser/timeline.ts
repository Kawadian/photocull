import { useEffect, useState } from 'react'
import { message, request } from '../api'
import type { Asset, Page } from '../api'
import type { BrowserLocation } from './location'
import type { BrowserMedia } from './ResponsiveImage'

export type TimelineAsset = Pick<Asset, 'id' | 'kind' | 'day' | 'width' | 'height' | 'duration' |
  'content_hash' | 'preview_status'> & { library: string; name: string; blurhash?: string | null }

export function assetKey(asset: Pick<TimelineAsset, 'library' | 'id'>) {
  return asset.library + ':' + asset.id
}

export function libraryUrl(library: string) {
  return '/api/libraries/' + encodeURIComponent(library)
}

export function browseUrl(endpoint: 'timeline' | 'months', library: string, month = '', cursor?: string | null) {
  const query = new URLSearchParams()
  if (library) query.set('library', library)
  if (month) query.set('month', month)
  if (cursor) query.set('cursor', cursor)
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

export function useTimeline({ library, month }: BrowserLocation) {
  const [load, setLoad] = useState<Load>({ cursor: null })
  const [feed, setFeed] = useState<Feed>({ assets: [], next: null, completed: null })
  const busy = feed.completed !== load

  useEffect(() => {
    const controller = new AbortController()
    request<Page<TimelineAsset, string> & { media?: BrowserMedia }>(browseUrl('timeline', library, month, load.cursor), {
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
  }, [library, month, load])

  function loadMore() {
    if (busy) return
    if (feed.error) setLoad({ cursor: load.cursor })
    else if (feed.next) setLoad({ cursor: feed.next })
  }

  return { ...feed, busy, loadMore }
}
