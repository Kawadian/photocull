import { ChevronLeft, ChevronRight, ImageOff, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { assetName, mediaUrl, useRemote } from '../api'
import type { Asset } from '../api'
import type { Strings } from './strings'
import { libraryUrl } from './timeline'
import type { TimelineAsset } from './timeline'

export function Preview({ photo, asset, text, close, previous, next }: {
  photo: string
  asset?: TimelineAsset
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
  const show = source?.preview_status === 'done' && failed !== url

  useEffect(() => {
    const modal = dialog.current!
    modal.showModal()
    return () => modal.close()
  }, [])

  function retry() {
    setFailed('')
    setAttempt((value) => value + 1)
  }

  return <dialog className="pcb-preview" ref={dialog} aria-label={name ?? text.loadingPhoto}
    onCancel={(event) => { event.preventDefault(); close() }}
    onKeyDown={(event) => {
      if (event.key === 'ArrowLeft' && previous) { event.preventDefault(); previous() }
      if (event.key === 'ArrowRight' && next) { event.preventDefault(); next() }
    }}
    onClick={(event) => { if (event.target === event.currentTarget) close() }}>
    <div className="pcb-preview-top">
      <div><strong>{name}</strong>{name && <span>{library}{source?.kind === 'video' && ' · ' + text.video}</span>}</div>
      <button className="pcb-icon" type="button" aria-label={text.close} title={text.close} onClick={close}>
        <X size={23} aria-hidden="true" />
      </button>
    </div>
    <div className="pcb-preview-body">
      {show ? <img key={photo + ':' + attempt} src={url} alt={name} onError={() => setFailed(url)} /> :
        <div className="pcb-state" role="status">
          <ImageOff size={26} aria-hidden="true" />
          <p>{result?.error ? text.photoError
            : !source ? text.loadingPhoto
            : source.preview_status === 'pending' || source.preview_status === 'running'
              ? text.previewPending : text.previewError}</p>
          {(source || result?.error) && <button className="pcb-control" type="button" onClick={retry}>{text.retry}</button>}
        </div>}
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
