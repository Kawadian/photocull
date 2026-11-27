import { Film, ImageOff, Images } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'
import { mediaUrl } from '../api'
import { gap, layout, neighbor, rowAt } from './layout'
import type { BrowserLocation } from './location'
import { Blurhash, ResponsiveImage } from './ResponsiveImage'
import { formatDate } from './strings'
import type { Strings } from './strings'
import { assetKey, libraryUrl } from './timeline'
import type { TimelineAsset } from './timeline'

function Thumbnail({ asset, priority, text, recipe, width }: {
  asset: TimelineAsset; priority: boolean; text: Strings; recipe?: string | null; width: number
}) {
  const url = mediaUrl(libraryUrl(asset.library), asset, 'thumb')
  const [failed, setFailed] = useState('')
  const pending = asset.preview_status !== 'error' && asset.preview_status !== 'skipped'
  return <>
    {asset.blurhash && <Blurhash value={asset.blurhash} />}
    {(asset.preview_status === 'done' || recipe) && failed !== url ?
      <ResponsiveImage size="grid" recipe={recipe} sizes={width + 'px'} library={asset.library}
        asset={asset} alt="" priority={priority} onError={() => setFailed(url)} />
      : <span className="pcb-thumbnail-empty" aria-hidden="true"
          title={asset.preview_status !== 'done' && pending ? text.previewPending : text.previewError}>
          {pending && failed !== url ? <Images size={21} /> : <ImageOff size={21} />}
        </span>}
  </>
}

export function TimelineGrid({ assets, location, recipe, text, busy, canLoad, loadMore, onPhoto, onMonth, children }: {
  assets: TimelineAsset[]
  location: BrowserLocation
  recipe?: string | null
  text: Strings
  busy: boolean
  canLoad: boolean
  loadMore: () => void
  onPhoto: (asset: TimelineAsset, button: HTMLButtonElement) => void
  onMonth: (month: string) => void
  children: ReactNode
}) {
  const scroller = useRef<HTMLElement>(null)
  const gallery = useRef<HTMLDivElement>(null)
  const buttons = useRef(new Map<number, HTMLButtonElement>())
  const frame = useRef(0)
  const [bounds, setBounds] = useState({ width: 0, height: 0, top: 0 })
  const [focused, setFocused] = useState<number>()
  const geometry = useMemo(
    () => layout(assets, bounds.width, location.density), [assets, bounds.width, location.density],
  )
  const { rows, height, assetRows } = geometry
  const firstVisible = rowAt(rows, bounds.top)
  const overscan = Math.min(500, bounds.height / 2)
  const start = rowAt(rows, bounds.top - overscan)
  const end = rowAt(rows, bounds.top + bounds.height + overscan) + 1
  const focusedRow = focused === undefined ? undefined : assetRows[focused]
  const visible = rows.slice(start, end)
  const tabIndex = focused ?? visible.slice(firstVisible - start).find((row) => row.type === 'photos')
    ?.cells[0].index ?? visible.find((row) => row.type === 'photos')?.cells[0].index
  if (focusedRow !== undefined && focusedRow < start) visible.unshift(rows[focusedRow])
  if (focusedRow !== undefined && focusedRow >= end) visible.push(rows[focusedRow])
  const current = rows[firstVisible]
  const day = current?.type === 'photos' ? assets[current.cells[0].index].day : current?.day
  const month = day?.slice(0, 7)

  useEffect(() => {
    const viewport = scroller.current!
    const surface = gallery.current!
    function measure() {
      setBounds({ width: surface.clientWidth, height: viewport.clientHeight, top: viewport.scrollTop })
    }
    function scroll() {
      if (frame.current) return
      frame.current = requestAnimationFrame(() => {
        frame.current = 0
        setBounds((previous) => ({ ...previous, top: viewport.scrollTop }))
      })
    }
    const observer = new ResizeObserver(measure)
    observer.observe(viewport)
    observer.observe(surface)
    viewport.addEventListener('scroll', scroll, { passive: true })
    return () => {
      observer.disconnect()
      viewport.removeEventListener('scroll', scroll)
      cancelAnimationFrame(frame.current)
      frame.current = 0
    }
  }, [])

  useEffect(() => {
    onMonth(month ?? '')
  }, [month, onMonth])

  useEffect(() => {
    if (canLoad && !busy && bounds.height > 0 && height - bounds.top < bounds.height + 320) loadMore()
  }, [bounds.height, bounds.top, busy, canLoad, height, loadMore])

  function moveFocus(event: KeyboardEvent, index: number) {
    const target = neighbor(rows, assetRows[index], index, event.key, assets.length)
    if (target === null || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    event.preventDefault()
    if (target === index) return
    const row = rows[assetRows[target]]
    const viewport = scroller.current!
    if (row.top < viewport.scrollTop) viewport.scrollTop = Math.max(0, row.top - 4)
    if (row.top + row.height > viewport.scrollTop + viewport.clientHeight) {
      viewport.scrollTop = row.top + row.height - viewport.clientHeight + 4
    }
    setBounds((previous) => ({ ...previous, top: viewport.scrollTop }))
    setFocused(target)
    requestAnimationFrame(() => buttons.current.get(target)?.focus({ preventScroll: true }))
  }

  return <main className="pcb-scroller" ref={scroller} aria-label={text.photos} aria-busy={busy}>
    <div className="pcb-gallery" ref={gallery}>
      <div className="pcb-virtual" style={{ height, '--pcb-gap': gap + 'px' } as CSSProperties}
        onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(undefined) }}>
        {visible.map((row) => {
          const style = { top: row.top, height: row.height }
          if (row.type === 'year') return <h2 className="pcb-year" key={row.key} style={style}>{row.day.slice(0, 4)}</h2>
          if (row.type === 'month') return <h3 className="pcb-month" key={row.key} style={style}>
            {formatDate(row.day, location.lang, { month: 'long' })}
          </h3>
          if (row.type === 'day') return <h4 className="pcb-day" key={row.key} style={style}>
            <time dateTime={row.day}>{formatDate(row.day, location.lang, { weekday: 'long', month: 'short', day: 'numeric' })}</time>
          </h4>
          return <div className="pcb-row" key={row.key} style={style}>
            {row.cells.map(({ index, width }) => {
              const asset = assets[index]
              const name = asset.name + (location.library ? '' : ' · ' + asset.library)
                + (asset.kind === 'video' ? ' · ' + text.video : '')
              return <button key={assetKey(asset)} className="pcb-tile" style={{ width }} type="button"
                title={name} aria-label={name} aria-haspopup="dialog" tabIndex={index === tabIndex ? 0 : -1}
                ref={(node) => { if (node) buttons.current.set(index, node); else buttons.current.delete(index) }}
                onFocus={() => setFocused(index)} onKeyDown={(event) => moveFocus(event, index)}
                onClick={(event) => onPhoto(asset, event.currentTarget)}>
                <Thumbnail asset={asset} recipe={recipe} width={width} priority={index < 2} text={text} />
                {asset.kind === 'video' && <span className="pcb-video-chip" aria-hidden="true">
                  <Film size={13} />
                  {asset.duration != null && ' ' + Math.floor(asset.duration / 60) + ':'
                    + String(Math.floor(asset.duration % 60)).padStart(2, '0')}
                </span>}
              </button>
            })}
          </div>
        })}
      </div>
      {children}
    </div>
  </main>
}
