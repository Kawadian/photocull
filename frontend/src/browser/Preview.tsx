import {
  ChevronLeft, ChevronRight, ImageOff, Info, Maximize, Minimize, Pause, Play, Share2, X, ZoomIn, ZoomOut,
} from 'lucide-react'
import { useEffect, useEffectEvent, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { assetName, json, mediaUrl, request, useRemote } from '../api'
import type { Rating } from '../api'
import type { Album } from './collections'
import { Delivery, deliveryText } from './Delivery'
import type { FilterValues } from './filterValues'
import type { Language } from './location'
import { PhotoInformation } from './PhotoInformation'
import { PhotoPlane } from './PhotoPlane'
import type { PhotoZoom } from './PhotoPlane'
import { ResponsiveImage } from './ResponsiveImage'
import type { BrowserMedia } from './ResponsiveImage'
import type { Strings } from './strings'
import { assetKey, libraryUrl } from './timeline'
import type { TimelineAsset } from './timeline'
import { VideoPlayer } from './VideoPlayer'
import { constrainedConnection } from './viewer'
import type { LabelUpdate, PhotoLabels, ViewerAsset } from './viewer'
import './viewer.css'

export function Preview({ photo, asset, media, mediaLoading, text, lang, close, previous, next,
  neighbors, advance, onLabels, filterBy, openAlbum }: {
  photo: string
  asset?: TimelineAsset
  media?: BrowserMedia
  mediaLoading: boolean
  text: Strings
  lang: Language
  close: () => void
  previous?: () => void
  next?: () => void
  neighbors: TimelineAsset[]
  advance: (random: boolean) => boolean
  onLabels: (photo: string, labels: PhotoLabels) => void
  filterBy: (filter: FilterValues) => void
  openAlbum: (album: Album) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const viewer = useRef<HTMLDivElement>(null)
  const zoom = useRef<PhotoZoom>(null)
  const deliveryButton = useRef<HTMLButtonElement>(null)
  const nativeFullscreen = useRef(false)
  const [attempt, setAttempt] = useState(0)
  const [deliveryOpen, setDeliveryOpen] = useState(false)
  const [revision, refresh] = useState(0)
  const [failed, setFailed] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [information, showInformation] = useState(false)
  const [showFaces, setShowFaces] = useState(false)
  const [rating, setRating] = useState<{ photo: string; value: Rating }>()
  const [ratingFailed, setRatingFailed] = useState('')
  const [saving, setSaving] = useState(false)
  const [labelState, setLabelState] = useState<{ photo: string; labels: PhotoLabels }>()
  const [labelSaving, setLabelSaving] = useState(false)
  const [labelFailed, setLabelFailed] = useState('')
  const [playing, setPlaying] = useState(false)
  const [seconds, setSeconds] = useState(5)
  const [random, setRandom] = useState(false)
  const [fade, setFade] = useState(true)
  const [library, assetId] = photo.split(':')
  const base = libraryUrl(library)
  const assetUrl = base + '/assets/' + assetId
  const result = useRemote<ViewerAsset>(assetUrl + '?details=browser', revision)
  const details = result?.data
  const rated = rating?.photo === photo && details && rating.value.revision >= details.revision
    ? { ...details, ...rating.value }
    : details
  const updated = rated && labelState?.photo === photo ? { ...rated, ...labelState.labels } : rated
  const source = updated ?? asset
  const name = updated ? assetName(updated) : asset?.name
  const url = source ? mediaUrl(base, source, 'preview') : ''
  const show = !mediaLoading && source?.preview_status === 'done' && failed !== url
  const isVideo = source?.kind === 'video'
  const indexing = updated?.exif_status === 'pending' || updated?.exif_status === 'running'
  const preload = neighbors.filter((item) => item.preview_status === 'done')
    .map((item) => mediaUrl(libraryUrl(item.library), item, 'thumb')).join(' ')

  useEffect(() => {
    const modal = dialog.current!
    modal.showModal()
    return () => modal.close()
  }, [])

  useEffect(() => {
    function changed() {
      if (document.fullscreenElement === viewer.current) nativeFullscreen.current = true
      else if (nativeFullscreen.current) {
        nativeFullscreen.current = false
        setExpanded(false)
      }
    }
    document.addEventListener('fullscreenchange', changed)
    return () => {
      document.removeEventListener('fullscreenchange', changed)
      if (nativeFullscreen.current && document.fullscreenElement) void document.exitFullscreen().catch(() => {})
    }
  }, [])

  useEffect(() => {
    if (!preload || constrainedConnection()) return
    for (const url of preload.split(' ')) {
      const image = new Image()
      image.fetchPriority = 'low'
      image.src = url
    }
  }, [preload])

  useEffect(() => {
    if (!information || !indexing) return
    const timer = setInterval(() => refresh((value) => value + 1), 2500)
    return () => clearInterval(timer)
  }, [information, indexing])

  const slide = useEffectEvent(() => {
    if (!advance(random)) setPlaying(false)
  })

  useEffect(() => {
    if (!playing) return
    const timer = setInterval(slide, seconds * 1000)
    return () => clearInterval(timer)
  }, [photo, playing, seconds])

  function fullscreen() {
    if (expanded) {
      if (document.fullscreenElement === viewer.current) void document.exitFullscreen().catch(() => {})
      setExpanded(false)
    } else {
      setExpanded(true)
      if (document.fullscreenEnabled) void viewer.current?.requestFullscreen().catch(() => {})
    }
  }

  function dismiss() {
    if (expanded) fullscreen()
    else close()
  }

  function retry() {
    setFailed('')
    setAttempt((value) => value + 1)
    refresh((value) => value + 1)
  }

  async function rate(value: number) {
    if (!updated || saving) return
    setSaving(true)
    setRatingFailed('')
    try {
      const response = await request<Rating>(assetUrl + '/rating', json('PATCH', {
        rating: value, rejected: false, expected_revision: updated.revision,
      }))
      setRating({ photo, value: response })
    } catch {
      setRatingFailed(photo)
      refresh((value) => value + 1)
    } finally {
      setSaving(false)
    }
  }

  async function editLabels(change: LabelUpdate) {
    if (labelSaving) return false
    setLabelSaving(true)
    setLabelFailed('')
    try {
      const url = '/api/browse/' + encodeURIComponent(library) + '/assets/' + assetId + '/labels'
      const labels = await request<PhotoLabels>(url, json('PATCH', change))
      setLabelState({ photo, labels })
      onLabels(photo, labels)
      return true
    } catch {
      setLabelFailed(photo)
      return false
    } finally {
      setLabelSaving(false)
    }
  }

  function keydown(event: KeyboardEvent) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.defaultPrevented) return
    if (event.target instanceof HTMLVideoElement || event.target instanceof HTMLSelectElement
      || event.target instanceof HTMLInputElement) return
    const key = event.key.toLowerCase()
    if (key === 'arrowleft' && previous) previous()
    else if (key === 'arrowright' && next) next()
    else if (key === 'f' && !event.repeat) fullscreen()
    else if (key === 'i' && !event.repeat) showInformation((open) => !open)
    else if (/^[0-5]$/.test(key) && !event.repeat) void rate(Number(key))
    else if (key === '+' || key === '=') zoom.current?.zoom(1.25)
    else if (key === '-' || key === '_') zoom.current?.zoom(0.8)
    else return
    event.preventDefault()
  }

  function selectSlide() {
    if (!playing && isVideo) {
      if (!advance(random)) return
    }
    setPlaying(!playing)
  }

  const still = show ? isVideo
    ? <ResponsiveImage key={photo + ':' + attempt} asset={source} library={library} size="fit"
        recipe={media?.recipe} alt={name ?? ''} onError={() => setFailed(url)}
        sizes="(max-width: 700px) 100vw, (max-width: 1280px) calc(100vw - 40px), 1240px" />
    : <PhotoPlane ref={zoom} key={photo + ':' + attempt} library={library}
        source={source} name={name ?? ''} recipe={media?.recipe} blurhash={asset?.blurhash}
        boxes={showFaces && information ? updated?.faces ?? [] : []} text={text}
        previous={previous} next={next} fade={playing && fade} onError={() => setFailed(url)} />
    : <div className="pcb-state" role="status">
        <ImageOff size={26} aria-hidden="true" />
        <p>{mediaLoading ? text.loadingPhoto : result?.error ? text.photoError : !source ? text.loadingPhoto
          : source.preview_status === 'pending' || source.preview_status === 'running'
            ? text.previewPending : text.previewError}</p>
        {!mediaLoading && (source || result?.error) &&
          <button className="pcb-control" type="button" onClick={retry}>{text.retry}</button>}
      </div>

  return <dialog className={'pcb-preview' + (expanded ? ' pcb-expanded' : '')} ref={dialog}
    aria-label={name ?? text.loadingPhoto}
    onCancel={(event) => { event.preventDefault(); dismiss() }} onKeyDown={keydown}
    onClick={(event) => { if (event.target === event.currentTarget) dismiss() }}>
    <div className="pcb-viewer" ref={viewer} data-photo={asset ? assetKey(asset) : photo}>
      <div className="pcb-preview-top">
        <div className="pcb-preview-title">
          <strong>{name}</strong>{name && <span>{library}{isVideo && ' · ' + text.video}</span>}
        </div>
        <div className="pcb-preview-actions">
          <button className="pcb-icon" type="button" aria-label={text.information} aria-keyshortcuts="I"
            aria-expanded={information} title={text.information + ' (I)'} onClick={() => showInformation(!information)}>
            <Info size={20} aria-hidden="true" />
          </button>
          <button className="pcb-icon" type="button" aria-label={expanded ? text.exitFullscreen : text.fullscreen}
            aria-keyshortcuts="F" aria-pressed={expanded} title={text.fullscreen + ' (F)'} onClick={fullscreen}>
            {expanded ? <Minimize size={20} aria-hidden="true" /> : <Maximize size={20} aria-hidden="true" />}
          </button>
          {source && media && (media.recipe || media.original_download) &&
            <button className="pcb-icon" type="button" ref={deliveryButton} aria-haspopup="dialog"
              aria-label={deliveryText[lang].title} title={deliveryText[lang].title} onClick={() => {
                setPlaying(false)
                setDeliveryOpen(true)
              }}><Share2 size={19} aria-hidden="true" /></button>}
          <button className="pcb-icon" type="button" aria-label={text.close} title={text.close} onClick={close}
            autoFocus><X size={23} aria-hidden="true" /></button>
        </div>
      </div>
      <div className="pcb-viewer-layout">
        <div className="pcb-preview-body">
          {isVideo && source && media && (media.recipe || media.original_streaming)
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
        {information && <PhotoInformation key={photo} asset={updated} text={text} lang={lang} boxes={showFaces}
          toggleBoxes={() => setShowFaces(!showFaces)} loading={indexing} error={result?.error}
          retry={retry} rate={(value) => { void rate(value) }} busy={saving} editLabels={editLabels}
          labelSaving={labelSaving} labelFailed={labelFailed === photo} filterBy={filterBy}
          albumAsset={{ library, id: Number(assetId) }} openAlbum={openAlbum} />}
      </div>
      <div className="pcb-viewer-footer">
        {!isVideo && <>
          <button className="pcb-icon" type="button" disabled={!show} aria-label={text.zoomOut}
            aria-keyshortcuts="-" title={text.zoomOut} onClick={() => zoom.current?.zoom(0.8)}>
            <ZoomOut size={18} aria-hidden="true" />
          </button>
          <button className="pcb-icon" type="button" disabled={!show} aria-label={text.zoomIn}
            aria-keyshortcuts="+" title={text.zoomIn} onClick={() => zoom.current?.zoom(1.25)}>
            <ZoomIn size={18} aria-hidden="true" />
          </button>
          <button className="pcb-control pcb-scale" type="button" disabled={!show}
            aria-label={text.nativeZoom} title={text.nativeZoom} onClick={() => zoom.current?.native()}>100%</button>
        </>}
        <button className="pcb-icon" type="button" disabled={!next && !previous}
          aria-label={playing ? text.pauseSlideshow : text.slideshow}
          title={playing ? text.pauseSlideshow : text.slideshow} onClick={selectSlide}>
          {playing ? <Pause size={18} aria-hidden="true" /> : <Play size={18} aria-hidden="true" />}
        </button>
        <details className="pcb-slideshow">
          <summary>{text.slideshowOptions}</summary>
          <div className="pcb-slideshow-menu">
            <label>{text.interval} <select value={seconds} onChange={(event) => setSeconds(Number(event.target.value))}>
              {[2, 3, 5, 10, 20].map((seconds) => <option key={seconds} value={seconds}>{seconds} s</option>)}
            </select></label>
            <label><input type="checkbox" checked={random} onChange={(event) => setRandom(event.target.checked)} /> {text.shuffle}</label>
            <label><input type="checkbox" checked={fade} onChange={(event) => setFade(event.target.checked)} /> {text.fade}</label>
          </div>
        </details>
        {ratingFailed === photo && <span role="alert">{text.ratingError}</span>}
      </div>
      {deliveryOpen && source && media && <Delivery key={photo} lang={lang} assetUrl={assetUrl}
        hash={source.content_hash} media={media} close={() => {
          setDeliveryOpen(false)
          requestAnimationFrame(() => deliveryButton.current?.focus())
        }} />}
    </div>
  </dialog>
}
