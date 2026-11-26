import {
  ArrowLeft, ArrowRight, ChevronLeft, ChevronRight, ImageOff, Images, Maximize, Scan, Star, Video, X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { assetName, mediaUrl, useRemote } from './api'
import type { Asset, Burst, GroupBy, Page, Rating } from './api'
import { ConfirmAction, ScoreBadges } from './Analysis'
import { Inspector } from './Inspector'

type ReviewProps = {
  base: string
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

function Preview({ base, asset, zoom, error, loading, filtered }: {
  base: string
  asset: Asset | undefined
  zoom: boolean
  error: string | undefined
  loading: boolean
  filtered?: boolean
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

  const heading = error ? 'Could not load assets'
    : loading ? 'Loading assets…'
    : !asset ? filtered ? 'No matching assets' : 'No assets here'
    : failed || asset.preview_status === 'error' ? 'Preview unavailable'
    : asset.preview_status === 'skipped' ? 'Preview skipped' : 'Preview queued'
  const detail = error || asset?.preview_error || (!asset && !loading && !filtered
    ? 'Run a library scan to add photos and videos.' : '')
  return (
    <div className="image-stage empty">
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
    filters, onChooseBest, onRejectRest, onClearFlag } = props
  const [cursors, setCursors] = useState([0])
  const [selected, setSelected] = useState<number | 'first' | 'last'>('first')
  const [zoom, setZoom] = useState(false)
  const [compare, setCompare] = useState(false)
  const [confirmReject, setConfirmReject] = useState<Burst>()
  const isBurst = by === 'burst'
  const params = new URLSearchParams(isBurst ? '' : filters)
  params.set('cursor', String(cursors.at(-1)))
  if (group !== null) params.set(isBurst ? 'burst_id' : by, group)
  const result = useRemote<Page<Asset>>(`${base}/assets?${params}`, version)
  const burstResult = useRemote<Burst>(isBurst && group !== null ? `${base}/bursts/${group}` : null, version)
  const burst = burstResult?.data
  const assets = (result?.data?.items ?? []).map((asset) => {
    const rating = ratings.get(asset.id)
    return rating && rating.revision > asset.revision ? { ...asset, ...rating } : asset
  })
  const nextCursor = result?.data?.next_cursor ?? null
  const indexById = assets.findIndex(({ id }) => id === selected)
  const index = selected === 'last' ? assets.length - 1 : Math.max(0, indexById)
  const asset = assets[index]
  const bestInPage = assets.find(({ id }) => id === burst?.best_id)
  const bestResult = useRemote<Asset>(
    compare && burst && !bestInPage ? `${base}/assets/${burst.best_id}` : null, version,
  )
  const best = bestInPage ?? bestResult?.data

  function move(direction: number) {
    const target = assets[index + direction]
    if (target && asset) {
      setSelected(target.id)
    } else if (direction < 0 && cursors.length > 1) {
      setCursors(cursors.slice(0, -1))
      setSelected('last')
    } else if (direction > 0 && nextCursor !== null) {
      setCursors([...cursors, nextCursor])
      setSelected('first')
    }
  }

  useEffect(() => {
    function keyboard(event: KeyboardEvent) {
      const target = event.target
      if (
        event.defaultPrevented || document.querySelector('dialog[open]') ||
        (target instanceof HTMLElement &&
          (target.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName)))
      ) return
      const key = event.key.toLowerCase()
      if (((event.ctrlKey || event.metaKey) && key === 'z') || key === 'u') {
        if (!event.repeat && undoAvailable && !busy) {
          event.preventDefault()
          onUndo()
        }
        return
      }
      if (event.ctrlKey || event.metaKey || event.altKey) return
      if (key === 'arrowleft' || key === 'arrowright') {
        event.preventDefault()
        move(key === 'arrowleft' ? -1 : 1)
      } else if (asset && !busy && !event.repeat) {
        if (/^[0-5]$/.test(key)) onRating(asset, Number(key))
        if (key === 'x') onRating(asset, 0, true)
        if (key === 'z' && asset.preview_status === 'done') setZoom((value) => !value)
      }
    }
    window.addEventListener('keydown', keyboard)
    return () => window.removeEventListener('keydown', keyboard)
  })

  const previousAvailable = index > 0 || cursors.length > 1
  const nextAvailable = index + 1 < assets.length || nextCursor !== null
  const heading = isBurst ? 'Burst review' : group === null ? 'All assets' : group || 'Library root'
  const image = (
    <Preview
      key={`${asset?.id}-${asset?.content_hash}-${asset?.preview_status}-${zoom}`}
      base={base}
      asset={asset}
      zoom={zoom}
      error={burstResult?.error || result?.error}
      loading={!result}
      filtered={!!filters || isBurst}
    />
  )

  return (
    <>
      <main className="review" aria-label="Photo review">
        <div className="review-header">
          <div className="review-bar">
            <div className="review-title">
              <h1 title={heading}>{heading}</h1>
              {asset && <p>{assetName(asset)}</p>}
            </div>
            <div className="review-tools">
              <div className="segment zoom" aria-label="Preview zoom">
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
                  aria-label="Previous asset" title="Previous asset (←)"><ArrowLeft size={17} /></button>
                <button className="icon-button" disabled={!nextAvailable} onClick={() => move(1)}
                  aria-label="Next asset" title="Next asset (→)"><ArrowRight size={17} /></button>
              </div>
            </div>
          </div>
          {burst && (
            <div className="burst-toolbar">
              <button className="button secondary" aria-pressed={compare} disabled={!asset}
                onClick={() => setCompare(!compare)}>Compare with best</button>
              <button className="button secondary" disabled={busy || !asset || burst.best_id === asset.id}
                onClick={() => asset && onChooseBest(burst, asset.id)}>Set as best</button>
              <button className="button secondary reject" disabled={busy}
                onClick={() => setConfirmReject(burst)}>Reject rest…</button>
              {asset?.id === burst.best_id && <span className="score-badge">
                {burst.user_best_id === asset.id ? 'Selected best' : 'Recommended best'}
              </span>}
              {asset && asset.id !== burst.best_id && asset.id === burst.recommended_best_id &&
                <span className="score-badge">Recommended</span>}
            </div>
          )}
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
            {(cursors.length > 1 || nextCursor !== null) && (
              <div className="filmstrip-pages">
                <button className="icon-button" aria-label="Previous asset page"
                  disabled={cursors.length < 2}
                  onClick={() => { setCursors(cursors.slice(0, -1)); setSelected('first') }}
                ><ChevronLeft size={16} /></button>
                <span>Page {cursors.length}</span>
                <button className="icon-button" aria-label="Next asset page"
                  disabled={nextCursor === null}
                  onClick={() => {
                    if (nextCursor !== null) { setCursors([...cursors, nextCursor]); setSelected('first') }
                  }}
                ><ChevronRight size={16} /></button>
              </div>
            )}
          </div>
          <Filmstrip assets={assets} active={asset} bestId={burst?.best_id}
            base={base} select={(value) => setSelected(value.id)} />
        </div>
      </main>
      <Inspector base={base} asset={asset} busy={busy} undoAvailable={undoAvailable}
        onRating={onRating} onUndo={onUndo} onClearFlag={onClearFlag} />
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
