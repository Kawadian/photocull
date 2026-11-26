import {
  ArrowLeft, ArrowRight, ChevronLeft, ChevronRight, ImageOff, Images, Maximize, Scan, Star, Video, X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { assetName, mediaUrl, useRemote } from './api'
import type { Asset, Burst, GroupBy, Page, Progress, Rating } from './api'
import { ConfirmAction, ScoreBadges } from './Analysis'
import { Inspector } from './Inspector'
import { usePhotoSearch } from './ML'
import type { Lookup } from './ML'
import { useShortcuts } from './shortcuts'

type ReviewProps = {
  base: string
  startAsset?: number
  onVideo?: () => void
  group: string | null
  by: GroupBy
  version: number
  ratings: Map<number, Rating>
  busy: boolean
  undoAvailable: boolean
  onRating: (asset: Asset, rating: number, rejected?: boolean) => void
  onUndo: () => void
  filters: string
  onChooseBest: (burst: Burst, assetId: number) => void
  onRejectRest: (burst: Burst) => Promise<void>
  onClearFlag: (asset: Asset) => void
  lookup?: Lookup
  onCloseLookup: () => void
  ml?: Progress['ml']
  workerOnline: boolean
  headingRef: RefObject<HTMLHeadingElement | null>
  similarRef: RefObject<HTMLButtonElement | null>
  onSimilar: (asset: Asset) => void
  onTag: (tag: string) => void
  activeTag: string
}

function Filmstrip({ base, assets, active, bestId, select }: {
  base: string
  assets: Asset[]
  active: Asset | undefined
  bestId?: number
  select: (asset: Asset) => void
}) {
  const rail = useRef<HTMLDivElement>(null)
  const selected = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!rail.current || !selected.current) return
    const viewport = rail.current.getBoundingClientRect()
    const cell = selected.current.getBoundingClientRect()
    if (cell.left < viewport.left || cell.right > viewport.right) {
      rail.current.scrollLeft += cell.left - viewport.left - (viewport.width - cell.width) / 2
    }
  }, [active?.id])

  return (
    <div className="filmstrip" aria-label="Filmstrip" ref={rail}>
      {assets.map((asset) => (
        <button
          className="frame"
          key={asset.id}
          ref={active?.id === asset.id ? selected : undefined}
          aria-current={active?.id === asset.id ? 'true' : undefined}
          aria-label={`${assetName(asset)}${asset.id === bestId ? ', best photo' : ''}${asset.auto_reject ? ', flagged' : ''}`}
          title={assetName(asset)}
          onClick={() => select(asset)}
        >
          {asset.preview_status === 'done'
            ? <img src={mediaUrl(base, asset, 'thumb')} alt="" loading="lazy" />
            : asset.kind === 'video' ? <Video size={20} /> : <Images size={20} />}
          {asset.rejected && <span className="frame-rating rejected"><X size={12} /></span>}
          {!!asset.rating && <span className="frame-rating"><Star size={11} />{asset.rating}</span>}
          {asset.auto_reject && <span className="frame-flag" aria-hidden="true">Flagged</span>}
          {asset.id === bestId && <span className="frame-best" aria-hidden="true">Best</span>}
        </button>
      ))}
    </div>
  )
}

function Preview({ base, asset, zoom, error, loading, filtered, searching }: {
  base: string
  asset: Asset | undefined
  zoom: boolean
  error: string | undefined
  loading: boolean
  filtered?: boolean
  searching?: boolean
}) {
  const viewer = useRef<HTMLDivElement>(null)
  const [failed, setFailed] = useState(false)

  if (asset?.preview_status === 'done' && !failed) {
    return (
      <div className={`image-stage ${zoom ? 'native' : 'fit'}`} ref={viewer}>
        <img
          key={String(zoom)}
          src={mediaUrl(base, asset, zoom ? 'detail' : 'preview')}
          alt={assetName(asset)}
          onError={() => setFailed(true)}
          onLoad={() => {
            if (zoom && viewer.current) {
              const { scrollWidth, scrollHeight, clientWidth, clientHeight } = viewer.current
              viewer.current.scrollTo((scrollWidth - clientWidth) / 2, (scrollHeight - clientHeight) / 2)
            }
          }}
        />
      </div>
    )
  }

  const heading = error ? searching ? 'Could not search photos' : 'Could not load assets'
    : loading ? searching ? 'Searching photos…' : 'Loading assets…'
    : !asset ? searching ? 'No matching photos' : filtered ? 'No matching assets' : 'No assets here'
    : failed || asset.preview_status === 'error' ? 'Preview unavailable'
    : asset.preview_status === 'skipped' ? 'Preview skipped' : 'Preview queued'
  const detail = error || asset?.preview_error || (!asset && !loading && !filtered
    ? 'Run a library scan to add photos and videos.' : '')
  return (
    <div className="image-stage empty" role={error ? 'alert' : 'status'}>
      <div>
        {asset ? <ImageOff size={30} /> : <Images size={30} />}
        <h2>{heading}</h2>
        {detail && <p>{detail}</p>}
      </div>
    </div>
  )
}

export function Review(props: ReviewProps) {
  const { base, group, by, version, ratings, busy, undoAvailable, onRating, onUndo,
    filters, onChooseBest, onRejectRest, onClearFlag, lookup, onCloseLookup, ml,
    onSimilar, onTag, activeTag, workerOnline, headingRef, similarRef } = props
  const [cursors, setCursors] = useState([props.startAsset ? props.startAsset - 1 : 0])
  const [browseSelection, setBrowseSelection] = useState<number | 'first' | 'last'>('first')
  const [searchSelection, setSearchSelection] = useState<{ lookup: Lookup; id: number }>()
  const [zoom, setZoom] = useState(false)
  const [retry, setRetry] = useState(0)
  const [compare, setCompare] = useState(false)
  const [confirmReject, setConfirmReject] = useState<Burst>()
  const isBurst = by === 'burst' && !lookup
  const isSimilar = by === 'similar'
  const params = new URLSearchParams(isBurst ? '' : filters)
  params.set('cursor', String(cursors.at(-1)))
  if (group !== null) params.set(isBurst ? 'burst_id' : isSimilar ? 'similar_group_id' : by, group)
  const browse = useRemote<Page<Asset>>(lookup ? null : `${base}/assets?${params}`, version + retry)
  const search = usePhotoSearch(base, lookup, retry)
  const result = lookup ? search : browse
  const selected = lookup ? searchSelection?.lookup === lookup ? searchSelection.id : 'first' : browseSelection
  const burstResult = useRemote<Burst>(isBurst && group !== null ? `${base}/bursts/${group}` : null, version + retry)
  const loadError = burstResult?.error ?? result?.error
  const loading = !result || (isBurst && !burstResult)
  const burst = loadError ? undefined : burstResult?.data
  const assets = (loadError ? [] : result?.data?.items ?? []).map((asset) => {
    const rating = ratings.get(asset.id)
    return rating && rating.revision > asset.revision ? { ...asset, ...rating } : asset
  })
  const nextCursor = result?.data?.next_cursor ?? null
  const indexById = assets.findIndex(({ id }) => id === selected)
  const index = selected === 'last' ? assets.length - 1 : Math.max(0, indexById)
  const asset = assets[index]
  const bestInPage = assets.find(({ id }) => id === burst?.best_id)
  const bestResult = useRemote<Asset>(
    compare && burst && !bestInPage ? `${base}/assets/${burst.best_id}` : null, version + retry,
  )
  const best = bestInPage ?? bestResult?.data
  useEffect(() => {
    if (lookup) headingRef.current?.focus()
  }, [headingRef, lookup])

  function select(asset: Asset) {
    if (lookup) setSearchSelection({ lookup, id: asset.id })
    else setBrowseSelection(asset.id)
  }

  function move(direction: number) {
    if (loading || (loadError && direction > 0)) return
    const target = assets[index + direction]
    if (target && asset) {
      select(target)
    } else if (!lookup && direction < 0 && cursors.length > 1) {
      setCursors(cursors.slice(0, -1))
      setBrowseSelection('last')
    } else if (!lookup && direction > 0 && nextCursor !== null) {
      setCursors([...cursors, nextCursor])
      setBrowseSelection('first')
    }
  }

  const canChooseBest = !busy && !!burst && !!asset && burst.best_id !== asset.id
  const canRejectRest = !busy && !!burst && !loading

  useShortcuts((key, event) => {
    const ratingUndo = !event.shiftKey && !event.altKey &&
      (((event.ctrlKey || event.metaKey) && key === 'z') ||
        (!event.ctrlKey && !event.metaKey && key === 'u'))
    if (ratingUndo) {
      if (!event.repeat && undoAvailable && !busy) {
        event.preventDefault()
        onUndo()
      }
      return
    }
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return
    if (key === 'arrowleft' || key === 'arrowright') {
      event.preventDefault()
      move(key === 'arrowleft' ? -1 : 1)
    } else if (!busy && !event.repeat) {
      if (key === 'r' && canRejectRest) {
        event.preventDefault()
        setConfirmReject(burst)
      }
      if (!asset) return
      if (key === 'b' && canChooseBest) {
        event.preventDefault()
        onChooseBest(burst, asset.id)
      }
      if (/^[0-5]$/.test(key)) onRating(asset, Number(key))
      if (key === 'x') onRating(asset, 0, true)
      if (key === 'z' && asset.preview_status === 'done') setZoom((value) => !value)
    }
  })

  const previousAvailable = !loading && (index > 0 || (!lookup && cursors.length > 1))
  const nextAvailable = !loading && !loadError && (index + 1 < assets.length || nextCursor !== null)
  const heading = lookup?.title ?? (isBurst ? 'Burst review' : isSimilar ? 'Similar group'
    : group === null ? 'All assets' : group || 'Library root')
  const image = (
    <Preview
      key={`${asset?.id}-${asset?.content_hash}-${asset?.preview_status}-${zoom}`}
      base={base}
      asset={asset}
      zoom={zoom}
      error={loadError}
      loading={loading}
      filtered={!!filters || isBurst || isSimilar}
      searching={!!lookup}
    />
  )

  return (
    <>
      <main className="review" aria-label="Photo review">
        <div className="review-header">
          <div className="review-bar">
            <div className="review-title">
              <h1 title={heading} ref={headingRef} tabIndex={-1}>{heading}</h1>
              {asset && <p>{assetName(asset)}
                {asset.similarity !== undefined && <> · Cosine {asset.similarity.toFixed(2)}</>}
              </p>}
            </div>
            <div className="review-tools">
              {lookup && <button className="icon-button" aria-label="Close photo search"
                aria-keyshortcuts="Escape" title="Back to review (Esc)" onClick={onCloseLookup}><X size={16} /></button>}
              <div className="segment zoom" role="group" aria-label="Preview zoom">
                <button
                  aria-pressed={!zoom}
                  title="Fit image"
                  disabled={asset?.preview_status !== 'done'}
                  onClick={() => setZoom(false)}
                ><Maximize size={14} /> Fit</button>
                <button
                  aria-pressed={zoom}
                  title="100% zoom (Z)"
                  disabled={asset?.preview_status !== 'done'}
                  onClick={() => setZoom(true)}
                ><Scan size={15} /> 100%</button>
              </div>
              <div className="arrow-buttons">
                <button className="icon-button" disabled={!previousAvailable} onClick={() => move(-1)}
                  aria-label="Previous asset" aria-keyshortcuts="ArrowLeft" title="Previous asset (←)"><ArrowLeft size={17} /></button>
                <button className="icon-button" disabled={!nextAvailable} onClick={() => move(1)}
                  aria-label="Next asset" aria-keyshortcuts="ArrowRight" title="Next asset (→)"><ArrowRight size={17} /></button>
              </div>
            </div>
          </div>
          {lookup?.source && <section className="similar-source" aria-label="Source photo">
            {lookup.source.preview_status === 'done' &&
              <img src={mediaUrl(base, lookup.source, 'thumb')} alt="" />}
            <div><span className="section-heading">Source photo</span>
              <div title={lookup.source.path}>{assetName(lookup.source)}</div>
            </div>
          </section>}
          {lookup && result?.data && <p className="lookup-count" role="status">
            {assets.length} {assets.length === 1 ? 'match' : 'matches'}
          </p>}
          {burst && (
            <div className="burst-toolbar" role="group" aria-label="Burst actions">
              <button className="button secondary" aria-pressed={compare} disabled={!asset}
                onClick={() => setCompare(!compare)}>Compare with best</button>
              <button className="button secondary" disabled={!canChooseBest} aria-keyshortcuts="B"
                onClick={() => asset && onChooseBest(burst, asset.id)}>Set as best <kbd aria-hidden="true">B</kbd></button>
              <button className="button secondary reject" disabled={!canRejectRest} aria-keyshortcuts="R"
                onClick={() => setConfirmReject(burst)}>Reject rest… <kbd aria-hidden="true">R</kbd></button>
              {asset?.id === burst.best_id && <span className="score-badge">
                {burst.user_best_id === asset.id ? 'Selected best' : 'Recommended best'}
              </span>}
              {asset && asset.id !== burst.best_id && asset.id === burst.recommended_best_id &&
                <span className="score-badge">Recommended</span>}
            </div>
          )}
          {loadError && <div className="review-retry">
            <button className="button secondary" onClick={() => setRetry((value) => value + 1)}>
              {lookup ? 'Retry search' : 'Retry review'}
            </button>
          </div>}
        </div>
        {compare && burst && asset && asset.id !== burst.best_id ? (
          <div className="comparison">
            <div className="comparison-frame">
              <div className="comparison-caption"><strong>Current photo</strong><ScoreBadges asset={asset} /></div>
              {image}
            </div>
            <div className="comparison-frame">
              <div className="comparison-caption"><strong>{best ? `Best · ${assetName(best)}` : 'Best photo'}</strong>
                {best && <ScoreBadges asset={best} />}
              </div>
              <Preview key={`${best?.id}-${best?.content_hash}-${best?.preview_status}-${zoom}`}
                base={base} asset={best} zoom={zoom} error={bestResult?.error} loading={!best && !bestResult} />
            </div>
          </div>
        ) : image}

        <div className="filmstrip-panel">
          <div className="filmstrip-bar">
            <span>Filmstrip</span>
            {!!assets.length && <span>{index + 1} / {assets.length}</span>}
            {!lookup && (cursors.length > 1 || nextCursor !== null) && (
              <div className="filmstrip-pages">
                <button className="icon-button" aria-label="Previous asset page"
                  disabled={cursors.length < 2}
                  onClick={() => { setCursors(cursors.slice(0, -1)); setBrowseSelection('first') }}
                ><ChevronLeft size={16} /></button>
                <span>Page {cursors.length}</span>
                <button className="icon-button" aria-label="Next asset page"
                  disabled={!nextAvailable || nextCursor === null}
                  onClick={() => {
                    if (nextCursor !== null) { setCursors([...cursors, nextCursor]); setBrowseSelection('first') }
                  }}
                ><ChevronRight size={16} /></button>
              </div>
            )}
          </div>
          <Filmstrip assets={assets} active={asset} bestId={burst?.best_id}
            base={base} select={select} />
        </div>
      </main>
      <Inspector onVideo={props.onVideo} base={base} asset={asset} busy={busy} undoAvailable={undoAvailable}
        onRating={onRating} onUndo={onUndo} onClearFlag={onClearFlag}
        ml={ml} onSimilar={onSimilar} onTag={onTag} activeTag={activeTag}
        workerOnline={workerOnline} similarRef={similarRef} />
      {confirmReject && <ConfirmAction
        title="Reject the other photos in this burst?"
        description="Apply a reversible flag to each of the other photos. You can undo this action after confirming."
        action="Flag remaining photos"
        onConfirm={() => onRejectRest(confirmReject)}
        onClose={() => setConfirmReject(undefined)}
      />}
    </>
  )
}
