import { Play } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import type { Asset } from '../api'
import { mediaUrl } from '../api'
import { derivativeUrl } from './ResponsiveImage'
import type { BrowserMedia } from './ResponsiveImage'
import type { Strings } from './strings'
import { libraryUrl } from './timeline'

export function VideoPlayer({ library, asset, media, text, children }: {
  library: string
  asset: Pick<Asset, 'id' | 'content_hash' | 'width' | 'height'>
  media: BrowserMedia
  text: Strings
  children: ReactNode
}) {
  const [quality, setQuality] = useState(media.recipe ? '480' : 'original')
  const [start, setStart] = useState(0)
  const [response, setResponse] = useState<{ key: string; status: 'ready' | 'error' }>()
  const base = libraryUrl(library)
  const url = quality === 'original' ? base + '/assets/' + asset.id + '/original?mode=stream'
    : media.recipe ? derivativeUrl(base, asset, 'video-' + quality + '.mp4', media.recipe) : ''
  const key = url + ':' + start
  const status = response?.key === key ? response.status : 'pending'
  const playable = !!start && (quality === 'original' || status === 'ready') && status !== 'error'

  useEffect(() => {
    if (!start || !url || quality === 'original') return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function check() {
      try {
        const pending = await fetch(url, { method: 'HEAD', signal: controller.signal })
        if (controller.signal.aborted) return
        if (pending.status === 202 || pending.status === 503) {
          const seconds = Number(pending.headers.get('Retry-After')) || 2
          timer = setTimeout(check, Math.max(2, Math.min(10, seconds)) * 1_000)
        } else {
          setResponse({ key, status: pending.ok ? 'ready' : 'error' })
        }
      } catch {
        if (!controller.signal.aborted) setResponse({ key, status: 'error' })
      }
    }
    void check()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [key, quality, start, url])

  return <>
    {playable ? <video src={url} controls playsInline preload="metadata"
      poster={mediaUrl(base, asset, 'preview')}
      onError={() => setResponse({ key, status: 'error' })} /> : children}
    <div className="pcb-playback">
      <select aria-label={text.videoQuality} value={quality} onChange={(event) => setQuality(event.target.value)}>
        {media.recipe && <><option value="480">480p</option><option value="720">720p</option></>}
        {media.original_streaming && <option value="original">{text.original}</option>}
      </select>
      {!start ? <button type="button" className="pcb-icon" aria-label={text.prepareVideo}
        title={text.prepareVideo} onClick={() => setStart(1)}><Play size={19} aria-hidden="true" /></button>
        : !playable && (status === 'error'
          ? <button type="button" className="pcb-control" onClick={() => setStart(start + 1)}>{text.retry}</button>
          : <span role="status">{text.preparingVideo}</span>)}
    </div>
  </>
}
