import { useEffect, useImperativeHandle, useRef, useState } from 'react'
import type { PointerEvent, Ref } from 'react'
import { mediaUrl } from '../api'
import type { Asset } from '../api'
import { Blurhash, derivativeUrl, ResponsiveImage } from './ResponsiveImage'
import type { Strings } from './strings'
import { libraryUrl } from './timeline'
import { constrainedConnection, useDerivativeAvailability } from './viewer'
import type { FaceBox } from './viewer'

type Point = { x: number; y: number }
type Transform = Point & { scale: number }
type Size = { width: number; height: number }
export type PhotoZoom = { zoom: (ratio: number) => void; native: () => void }
const FIT = { x: 0, y: 0, scale: 1 }

function distance(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

function midpoint(a: Point, b: Point) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

function clamp(value: number, lower: number, upper: number) {
  return Math.min(upper, Math.max(lower, value))
}

export function PhotoPlane({ ref, source, library, name, recipe, blurhash, boxes, text, onError, previous, next, fade }: {
  ref: Ref<PhotoZoom>
  source: Pick<Asset, 'id' | 'width' | 'height' | 'content_hash'>
  library: string
  name: string
  recipe?: string | null
  blurhash?: string | null
  boxes: FaceBox[]
  text: Strings
  onError: () => void
  previous?: () => void
  next?: () => void
  fade: boolean
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const [bounds, setBounds] = useState<Size>({ width: 0, height: 0 })
  const [transform, setTransform] = useState<Transform>(FIT)
  const current = useRef<Transform>(FIT)
  const pointers = useRef(new Map<number, Point>())
  const gesture = useRef<{ start: Point; at: number; pinched: boolean }>(null)
  const tap = useRef<{ at: number; point: Point }>(null)
  const [loaded, setLoaded] = useState(false)
  const [nativeLoaded, setNativeLoaded] = useState(false)
  const [nativeRequested, setNativeRequested] = useState(0)
  const [nativeFailed, setNativeFailed] = useState(false)
  const sourceWidth = source.width || 4
  const sourceHeight = source.height || 3
  const fit = Math.min(bounds.width / sourceWidth, bounds.height / sourceHeight)
  const width = fit * sourceWidth
  const height = fit * sourceHeight
  const base = libraryUrl(library)
  const wantNative = !!nativeRequested || (transform.scale >= 1.6 && !constrainedConnection())
  const nativeUrl = wantNative && recipe ? derivativeUrl(base, source, 'native.jpg', recipe) : ''
  const nativeStatus = useDerivativeAvailability(nativeUrl, nativeRequested)

  function apply(value: Transform) {
    const xMax = Math.max(0, (width * value.scale - bounds.width) / 2)
    const yMax = Math.max(0, (height * value.scale - bounds.height) / 2)
    const next = { scale: value.scale, x: clamp(value.x, -xMax, xMax), y: clamp(value.y, -yMax, yMax) }
    current.current = next
    setTransform(next)
  }

  function zoom(ratio: number, from: Point = { x: 0, y: 0 }, to: Point = from) {
    const view = current.current
    const scale = clamp(view.scale * ratio, 1, 8)
    const change = scale / view.scale
    apply({ scale, x: (view.x - from.x) * change + to.x, y: (view.y - from.y) * change + to.y })
  }

  useImperativeHandle(ref, () => ({
    zoom,
    native() {
      if (current.current.scale !== 1) apply(FIT)
      else {
        setNativeRequested((value) => value + 1)
        setNativeFailed(false)
        const scale = fit > 0 && source.width && source.height ? clamp(1 / fit, 0.125, 8) : 2
        apply({ ...FIT, scale })
      }
    },
  }))

  useEffect(() => {
    const stage = viewport.current!
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      if (width > 0 && height > 0) setBounds({ width, height })
    })
    observer.observe(stage)
    return () => observer.disconnect()
  }, [])

  function local(event: PointerEvent): Point {
    const rect = event.currentTarget.getBoundingClientRect()
    return { x: event.clientX - rect.left - rect.width / 2, y: event.clientY - rect.top - rect.height / 2 }
  }

  function down(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 && event.pointerType === 'mouse') return
    const point = local(event)
    if (!pointers.current.size) gesture.current = { start: point, at: performance.now(), pinched: false }
    else if (gesture.current) gesture.current.pinched = true
    pointers.current.set(event.pointerId, point)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function move(event: PointerEvent<HTMLDivElement>) {
    const previousPoint = pointers.current.get(event.pointerId)
    if (!previousPoint) return
    const point = local(event)
    pointers.current.set(event.pointerId, point)
    const other = [...pointers.current.entries()].find(([id]) => id !== event.pointerId)?.[1]
    if (other) {
      const initialDistance = distance(previousPoint, other)
      if (initialDistance > 0) zoom(distance(point, other) / initialDistance,
        midpoint(previousPoint, other), midpoint(point, other))
    } else if (current.current.scale > 1) {
      const view = current.current
      apply({ ...view, x: view.x + point.x - previousPoint.x, y: view.y + point.y - previousPoint.y })
    }
  }

  function up(event: PointerEvent<HTMLDivElement>, canceled = false) {
    if (!pointers.current.delete(event.pointerId) || pointers.current.size) return
    const start = gesture.current
    gesture.current = null
    if (!start || start.pinched || canceled) return
    const now = performance.now()
    const point = local(event)
    const dx = point.x - start.start.x
    const dy = point.y - start.start.y
    if (current.current.scale <= 1 && now - start.at < 700
      && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.4) {
      tap.current = null
      if (dx > 0) previous?.()
      else next?.()
      return
    }
    if (distance(point, start.start) > 12 || now - start.at > 300) return
    if (tap.current && now - tap.current.at < 330 && distance(point, tap.current.point) < 42) {
      if (current.current.scale !== 1) apply(FIT)
      else zoom(2.25, point)
      tap.current = null
    } else tap.current = { at: now, point }
  }

  return <div className="pcb-photo-viewport" ref={viewport}
    data-zoomed={transform.scale > 1 || undefined} onPointerDown={down} onPointerMove={move}
    onPointerUp={up} onPointerCancel={(event) => up(event, true)}>
    <div className={'pcb-photo-plane' + (fade ? ' pcb-photo-fade' : '')} style={{
      width, height, transform: 'translate3d(' + transform.x + 'px,' + transform.y + 'px,0) scale(' + transform.scale + ')',
    }}>
      {width > 0 && <>
        {blurhash && !loaded && <Blurhash value={blurhash} />}
        {!loaded && <img className="pcb-photo-low" src={mediaUrl(base, source, 'thumb')} alt="" draggable={false} />}
        <ResponsiveImage size="fit" recipe={recipe} sizes={Math.max(1, Math.round(width)) + 'px'}
          library={library} asset={source} alt={name} onError={onError} onLoad={() => setLoaded(true)} />
      </>}
      {nativeUrl && nativeStatus === 'ready' && !nativeFailed &&
        <img className="pcb-photo-native" data-loaded={nativeLoaded || undefined} src={nativeUrl} alt=""
          draggable={false} onLoad={() => setNativeLoaded(true)} onError={() => setNativeFailed(true)} />}
      {boxes.map((box) => <span className="pcb-face-box" key={box.id} aria-hidden="true" style={{
        left: box.left * 100 + '%', top: box.top * 100 + '%',
        width: (box.right - box.left) * 100 + '%', height: (box.bottom - box.top) * 100 + '%',
      }}>{box.name && <span>{box.name}</span>}</span>)}
    </div>
    {nativeUrl && (nativeStatus !== 'ready' || nativeFailed) &&
      <p className="pcb-quality-status" role="status">
        {nativeStatus === 'error' || nativeFailed ? text.nativeUnavailable : text.nativeLoading}
      </p>}
  </div>
}
