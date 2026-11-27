import { Play } from 'lucide-react'
import { useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { Asset } from '../api'
import { mediaUrl, useRemote } from '../api'
import { derivativeUrl } from './ResponsiveImage'
import type { BrowserMedia } from './ResponsiveImage'
import type { Strings } from './strings'
import { libraryUrl } from './timeline'
import { useDerivativeAvailability } from './viewer'

type VideoAnalysis = {
  video?: { data: { highlights: { start: number; end: number }[] } | null }
}

function timestamp(seconds: number) {
  return Math.floor(seconds / 60) + ':' + String(Math.floor(seconds % 60)).padStart(2, '0')
}

export function VideoPlayer({ library, asset, media, text, children }: {
  library: string
  asset: Pick<Asset, 'id' | 'content_hash' | 'width' | 'height'>
  media: BrowserMedia
  text: Strings
  children: ReactNode
}) {
  const [quality, setQuality] = useState(media.recipe ? '480' : 'original')
  const [start, setStart] = useState(0)
  const [failed, setFailed] = useState('')
  const player = useRef<HTMLVideoElement>(null)
  const seek = useRef(0)
  const base = libraryUrl(library)
  const url = quality === 'original' ? base + '/assets/' + asset.id + '/original?mode=stream'
    : media.recipe ? derivativeUrl(base, asset, 'video-' + quality + '.mp4', media.recipe) : ''
  const readiness = useDerivativeAvailability(start && quality !== 'original' ? url : '', start)
  const key = url + ':' + start
  const status = failed === key ? 'error' : readiness
  const playable = !!start && (quality === 'original' || status === 'ready') && status !== 'error'
  const analysis = useRemote<VideoAnalysis>(base + '/insights/assets/' + asset.id + '/video')?.data
  const highlights = analysis?.video?.data?.highlights ?? []

  function jump(seconds: number) {
    seek.current = seconds
    if (player.current?.readyState) player.current.currentTime = seconds
    if (!start) setStart(1)
  }

  return <>
    {playable ? <video src={url} controls playsInline preload="metadata" ref={player}
      poster={mediaUrl(base, asset, 'preview')}
      onLoadedMetadata={(event) => { event.currentTarget.currentTime = seek.current }}
      onError={() => setFailed(key)} /> : children}
    {!!highlights.length && <div className="pcb-highlights" role="group" aria-label={text.highlights}>
      <span>{text.highlights}</span>
      {highlights.map((highlight) => <button key={highlight.start} className="pcb-control" type="button"
        onClick={() => jump(highlight.start)}>{timestamp(highlight.start)}</button>)}
    </div>}
    <div className="pcb-playback">
      <select aria-label={text.videoQuality} value={quality} onChange={(event) => {
        if (player.current) seek.current = player.current.currentTime
        setQuality(event.target.value)
      }}>
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
