import { ChevronLeft, ChevronRight, Download, ImageOff, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { assetName, mediaUrl, useRemote } from '../api'
import type { Asset } from '../api'
import { ResponsiveImage } from './ResponsiveImage'
import type { BrowserMedia } from './ResponsiveImage'
import type { Strings } from './strings'
import { libraryUrl } from './timeline'
import type { TimelineAsset } from './timeline'
import { VideoPlayer } from './VideoPlayer'

export function Preview({ photo, asset, media, mediaLoading, text, close, previous, next }: {
  photo: string
  asset?: TimelineAsset
  media?: BrowserMedia
  mediaLoading: boolean
  text: Strings
  close: () => void
  previous?: () => void
  next?: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [attempt, setAttempt] = useState(0)
  const [failed, setFailed] = useState('')
  const [library, assetId] = photo.split(':')
  const base = libraryUrl(library)
  const result = useRemote<Asset>(asset && asset.preview_status === 'done' ? null : base + '/assets/' + assetId, attempt)
  const source = result?.data ?? asset
  const name = result?.data ? assetName(result.data) : asset?.name
  const url = source ? mediaUrl(base, source, 'preview') : ''
  const show = !mediaLoading && source?.preview_status === 'done' && failed !== url

  useEffect(() => {
    const modal = dialog.current!
    modal.showModal()
    return () => modal.close()
  }, [])

  function retry() {
    setFailed('')
    setAttempt((value) => value + 1)
  }

  const still = show ? <ResponsiveImage key={photo + ':' + attempt} asset={source} library={library} size="fit"
      recipe={media?.recipe} alt={name ?? ''} onError={() => setFailed(url)}
      sizes="(max-width: 700px) 100vw, (max-width: 1280px) calc(100vw - 40px), 1240px" /> :
    <div className="pcb-state" role="status">
      <ImageOff size={26} aria-hidden="true" />
      <p>{mediaLoading ? text.loadingPhoto
        : result?.error ? text.photoError
        : !source ? text.loadingPhoto
        : source.preview_status === 'pending' || source.preview_status === 'running'
          ? text.previewPending : text.previewError}</p>
      {!mediaLoading && (source || result?.error) &&
        <button className="pcb-control" type="button" onClick={retry}>{text.retry}</button>}
    </div>

  return <dialog className="pcb-preview" ref={dialog} aria-label={name ?? text.loadingPhoto}
    onCancel={(event) => { event.preventDefault(); close() }}
    onKeyDown={(event) => {
      if (event.target instanceof HTMLVideoElement || event.target instanceof HTMLSelectElement) return
      if (event.key === 'ArrowLeft' && previous) { event.preventDefault(); previous() }
      if (event.key === 'ArrowRight' && next) { event.preventDefault(); next() }
    }}
    onClick={(event) => { if (event.target === event.currentTarget) close() }}>
    <div className="pcb-preview-top">
      <div><strong>{name}</strong>{name && <span>{library}{source?.kind === 'video' && ' · ' + text.video}</span>}</div>
      <div className="pcb-preview-actions">
        {source && media?.original_download && <a className="pcb-icon" href={base + '/assets/' + assetId + '/original?include_location=true'}
          download aria-label={text.downloadOriginal} title={text.originalLocation}>
          <Download size={20} aria-hidden="true" />
        </a>}
        <button className="pcb-icon" type="button" aria-label={text.close} title={text.close} onClick={close}>
          <X size={23} aria-hidden="true" />
        </button>
      </div>
    </div>
    <div className="pcb-preview-body">
      {source?.kind === 'video' && media && (media.recipe || media.original_streaming)
        ? <VideoPlayer key={photo} library={library} asset={source} media={media} text={text}>{still}</VideoPlayer>
        : still}
      {(previous || next) && <div className="pcb-preview-navigation">
        <button className="pcb-icon" aria-label={text.previous} type="button" disabled={!previous} onClick={previous}>
          <ChevronLeft size={23} aria-hidden="true" />
        </button>
        <button className="pcb-icon" aria-label={text.next} type="button" disabled={!next} onClick={next}>
          <ChevronRight size={23} aria-hidden="true" />
        </button>
      </div>}
    </div>
  </dialog>
}
