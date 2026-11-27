import { Archive, CalendarDays, Film, FolderOutput, Image, Map, Sparkles, Users } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { assetName, json, mediaUrl, message, request, useRemote } from './api'
import type { Asset, Page } from './api'
import { ConfirmAction } from './Analysis'
import { useShortcuts } from './shortcuts'

export const views = [
  { id: 'review', label: 'Review', icon: Image },
  { id: 'faces', label: 'People', icon: Users },
  { id: 'story', label: 'Stories', icon: CalendarDays },
  { id: 'video', label: 'Videos', icon: Film },
  { id: 'taste', label: 'Recommendations', icon: Sparkles },
  { id: 'export', label: 'Exports', icon: FolderOutput },
  { id: 'report', label: 'Trip reports', icon: Map },
  { id: 'archive', label: 'Archive', icon: Archive },
] as const
export type View = typeof views[number]['id']
export type InsightStage = 'faces' | 'story' | 'video' | 'aesthetic' | 'transcript' | 'taste'
export type InsightCapabilities = {
  faces: boolean; story: boolean; video: boolean; aesthetic: boolean; transcript: boolean
  watch: boolean; export: boolean; report: boolean; destinations: string[]; strip_location?: boolean
  stages: Partial<Record<InsightStage, { pending: boolean; error: string | null }>>
  taste: { status: 'disabled' | 'pending' | 'ready' | 'insufficient' | 'error'
    positives: number; negatives: number; error: string | null }
}
export type OpenReview = (filters: string, label: string, id?: number) => void
type ScreenProps = {
  base: string; version: number; capability: InsightCapabilities; openReview: OpenReview
}
type Media = Pick<Asset, 'id' | 'path' | 'content_hash'>
type Box = { left: number; top: number; right: number; bottom: number }
type Face = Box & { id: number; asset_id: number; path: string; content_hash: string; quality: number }
type Person = { id: number; name: string | null; revision: number; count: number; cover: Box & {
  asset_id: number; content_hash: string
} }

export function Status({ error, pending, empty, children, retry }: {
  error?: string | null; pending?: boolean; empty?: boolean; children?: ReactNode; retry?: () => void
}) {
  if (!error && !pending && !empty) return null
  return <div className="insight-status" role={error ? 'alert' : 'status'}>
    {error || (pending ? 'Loading…' : children)}
    {error && retry && <button className="button secondary" onClick={retry}>Retry</button>}
  </div>
}

function Paging({ position, previous, next, forward }: {
  position: number; previous: () => void; next: number | string | null | undefined; forward: () => void
}) {
  if (!position && next == null) return null
  return <div className="insight-actions insight-paging" aria-label="Page controls">
    <button className="button secondary" disabled={!position} onClick={previous}>Previous page</button>
    <button className="button secondary" disabled={next == null} onClick={forward}>Next page</button>
  </div>
}

function usePageCursor<T>(initial: T) {
  const [stack, setStack] = useState([initial])
  return {
    cursor: stack.at(-1) ?? initial,
    position: stack.length - 1,
    previous: () => setStack((all) => all.length > 1 ? all.slice(0, -1) : all),
    forward: (value: T | null | undefined) => { if (value != null) setStack((all) => [...all, value]) },
    reset: () => setStack([initial]),
  }
}

function useAction(version: number) {
  const [refresh, setRefresh] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const retry = () => { setError(undefined); setRefresh((v) => v + 1) }
  async function action<T>(url: string, init: RequestInit, after?: (value: T) => void) {
    setBusy(true)
    setError(undefined)
    try {
      const data = await request<T>(url, init)
      after?.(data)
      retry()
    } catch (failure) {
      setError(message(failure))
      throw failure
    } finally {
      setBusy(false)
    }
  }
  return { version: version + refresh, busy, error, retry, action }
}

function FacePicture({ base, face, alt = '' }: {
  base: string; face: Box & { asset_id: number; content_hash: string }; alt?: string
}) {
  return <span className="face-picture">
    <img src={mediaUrl(base, { id: face.asset_id, content_hash: face.content_hash }, 'thumb')}
      alt={alt} loading="lazy" />
    <span className="face-box" aria-hidden="true" style={{
      left: `${face.left * 100}%`, top: `${face.top * 100}%`,
      width: `${(face.right - face.left) * 100}%`, height: `${(face.bottom - face.top) * 100}%`,
    }} />
  </span>
}

const personName = (person: Person) => person.name ?? `Person ${person.id}`

function useDetailHeading() {
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { heading.current?.focus() }, [])
  return heading
}

function PersonDetail({ base, initial, version, openReview, beginMerge, openPerson, close }: ScreenProps & {
  initial: Person; beginMerge: (person: Person) => void; openPerson: (person: Person) => void; close: () => void
}) {
  const [person, setPerson] = useState(initial)
  const heading = useDetailHeading()
  const [name, setName] = useState(initial.name ?? '')
  const [selected, setSelected] = useState<number[]>([])
  const cursor = usePageCursor(0)
  const work = useAction(version)
  const url = `${base}/insights/people/${person.id}`
  const result = useRemote<Page<Face> & { person: Pick<Person, 'id' | 'name' | 'revision'> }>(
    `${url}/faces?cursor=${cursor.cursor}`, work.version)
  const faces = result?.data?.items ?? []
  const current = { ...person, ...result?.data?.person }
  const available = !!result?.data && !result.error
  return <section className="insight-section" aria-label={personName(current)}>
    <h2 ref={heading} tabIndex={-1}>{personName(current)}</h2>
    <div className="insight-actions">
      <button className="button secondary" onClick={close}>All people</button>
      <button className="button secondary" disabled={!available} onClick={() =>
        openReview(`person_id=${person.id}`, personName(current))}>Review photos of {personName(current)}</button>
      <button className="button secondary" disabled={!available} onClick={() =>
        beginMerge(current)}>Merge into another person…</button>
    </div>
    <form className="insight-actions" onSubmit={(event) => {
      event.preventDefault()
      void work.action<Person>(url, json('PATCH', { name: name.trim() || null, expected_revision: person.revision }),
        (value) => setPerson({ ...person, ...value })).catch(() => {})
    }}>
      <label>Person name <input value={name} maxLength={120} onChange={(event) => setName(event.target.value)} /></label>
      <button className="button secondary" disabled={!available || work.busy || name.trim() === (person.name ?? '')}>
        Save name
      </button>
    </form>
    <Status error={work.error || result?.error} pending={!result} empty={!faces.length} retry={() => {
      setPerson(current)
      setName(current.name ?? '')
      setSelected([])
      work.retry()
    }}>
      This person has no indexed faces.
    </Status>
    {faces.length > 0 && <fieldset className="insight-fieldset">
      <legend>Select faces to split into a new person</legend>
      <div className="insight-grid">
        {faces.map((face, index) => <label className="insight-card selectable" key={face.id}>
          <FacePicture base={base} face={face} />
          <span><input type="checkbox" checked={selected.includes(face.id)}
            aria-label={`Face ${index + 1} on this page, ${assetName(face)}`}
            disabled={selected.length >= 200 && !selected.includes(face.id)} onChange={() =>
              setSelected((all) => all.includes(face.id) ? all.filter((id) => id !== face.id) : [...all, face.id])
            } /> {assetName(face)}</span>
        </label>)}
      </div>
      <button className="button primary" disabled={!selected.length || work.busy} onClick={() => {
        void work.action<{ id: number; moved: number }>(`${url}/split`, json('POST', { faces: selected }),
          ({ id, moved }) => openPerson({ ...person, id, name: null, revision: 0, count: moved })).catch(() => {})
      }}>Split {selected.length || ''} selected faces</button>
    </fieldset>}
    <Paging {...cursor} next={result?.data?.next_cursor}
      forward={() => cursor.forward(result?.data?.next_cursor)} />
  </section>
}

function People(props: ScreenProps) {
  const { base, version, capability } = props
  const [person, setPerson] = useState<Person>()
  const [source, setSource] = useState<Person>()
  const [target, setTarget] = useState<Person>()
  const cursor = usePageCursor(0)
  const work = useAction(version)
  const url = `${base}/insights/people`
  const result = useRemote<Page<Person>>(person ? null : `${url}?cursor=${cursor.cursor}`, work.version)
  const people = result?.data?.items ?? []
  if (person) return <PersonDetail key={person.id} {...props} initial={person} openPerson={setPerson} close={() => setPerson(undefined)}
    beginMerge={(entry) => { setSource(entry); setPerson(undefined) }} />
  return <>
    {source && <div className="insight-actions" role="status">
      Choose who should receive the faces from <strong>{personName(source)}</strong>.
      <button className="button secondary" onClick={() => setSource(undefined)}>Cancel merge</button>
    </div>}
    <Status error={result?.error || capability.stages.faces?.error}
      pending={!result || (!people.length && capability.stages.faces?.pending)} empty={!people.length}
      retry={work.retry}>No faces found in this library.</Status>
    <div className="insight-grid">
      {people.map((item) => <button className="insight-card" key={item.id} disabled={item.id === source?.id}
        onClick={() => source ? setTarget(item) : setPerson(item)}>
        <FacePicture base={base} face={item.cover} />
        <strong>{personName(item)}</strong>
        <span>{item.count} {item.count === 1 ? 'photo' : 'photos'}</span>
      </button>)}
    </div>
    <Paging {...cursor} next={result?.data?.next_cursor} forward={() => cursor.forward(result?.data?.next_cursor)} />
    {target && source && <ConfirmAction title={`Merge into ${personName(target)}?`}
      description={`Move all faces from ${personName(source)} into ${personName(target)}.`}
      action="Merge people" onClose={() => setTarget(undefined)} onConfirm={() =>
        work.action<Pick<Person, 'name' | 'revision'>>(`${url}/${target.id}/merge`, json('POST', {
          source: source.id, expected_revision: target.revision, expected_source_revision: source.revision,
        }), ({ name, revision }) => {
          setTarget(undefined)
          setSource(undefined)
          setPerson({ ...target, name, revision, count: target.count + source.count })
        })} />}
  </>
}

type StoryDay = { day: string; count: number; segments: number }
type Segment = {
  day: string; position: number; title: string | null; started: string | null; ended: string | null
  latitude: number | null; longitude: number | null; count: number; representatives: Media[]
}

function StoryMap({ items }: { items: Segment[] }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const context = canvas.current?.getContext('2d')
    if (!context) return
    context.clearRect(0, 0, 480, 144)
    const locations = items.flatMap(({ latitude, longitude }) =>
      latitude != null && longitude != null ? [[longitude, latitude]] : [])
    if (locations.length < 2) return
    const anchor = locations[0][0]
    const x = locations.map(([lon]) => ((lon - anchor + 540) % 360) - 180)
    const y = locations.map((point) => point[1])
    const sx = Math.max(...x) - Math.min(...x)
    const sy = Math.max(...y) - Math.min(...y)
    const scale = Math.min(428 / Math.max(sx, 0.0001), 104 / Math.max(sy, 0.0001))
    const points = x.map((lon, i) => [
      240 + (lon - (Math.max(...x) + Math.min(...x)) / 2) * scale,
      72 - (y[i] - (Math.max(...y) + Math.min(...y)) / 2) * scale,
    ])
    context.strokeStyle = '#b5e5c6'
    context.lineWidth = 2
    context.beginPath()
    points.forEach(([px, py], i) => i ? context.lineTo(px, py) : context.moveTo(px, py))
    context.stroke()
    context.fillStyle = '#edf0ef'
    for (const [px, py] of points) {
      context.beginPath()
      context.arc(px, py, 3.5, 0, Math.PI * 2)
      context.fill()
    }
  }, [items])
  if (items.filter((item) => item.latitude !== null).length < 2) return null
  return <canvas ref={canvas} className="story-map" width={480} height={144} role="img"
    aria-label="Relative positions of the story stops, connected in capture order" />
}

function MediaCards({ base, assets, open }: {
  base: string; assets: Media[]; open: (asset: Media) => void
}) {
  return <div className="insight-grid">
    {assets.map((asset) => <button className="insight-card" key={asset.id} onClick={() => open(asset)}>
      <img src={mediaUrl(base, asset, 'thumb')} alt="" loading="lazy" />
      <span>{assetName(asset)}</span>
    </button>)}
  </div>
}

function StoryDetail({ base, version, day, openReview, close }: ScreenProps & {
  day: string; close: () => void
}) {
  const work = useAction(version)
  const cursor = usePageCursor(0)
  const heading = useDetailHeading()
  const result = useRemote<Page<Segment>>(`${base}/insights/stories/${day}?cursor=${cursor.cursor}`,
    work.version)
  const items = result?.data?.items ?? []
  return <div className="insight-section">
    <div className="insight-actions">
      <button className="button secondary" onClick={close}>All stories</button>
      <button className="button secondary" onClick={() => openReview(`day=${day}`, day)}>Review this day</button>
    </div>
    <h2 ref={heading} tabIndex={-1}>{day}</h2>
    <Status error={result?.error} pending={!result} empty={!items.length} retry={work.retry}>
      This story is waiting for indexing.
    </Status>
    <StoryMap items={items} />
    {items.map((item) => <article className="story-segment" key={item.position}>
      <h3>{item.title ?? (item.latitude === null ? 'Location unavailable'
        : `${item.latitude.toFixed(3)}, ${item.longitude?.toFixed(3)}`)}</h3>
      <p className="insight-muted">
        {item.started ? `${item.started.slice(11, 16)} – ${item.ended?.slice(11, 16)}` : 'Capture time unavailable'}
        {' · '}{item.count} {item.count === 1 ? 'item' : 'items'}
      </p>
      <MediaCards base={base} assets={item.representatives}
        open={(asset) => openReview(`day=${day}`, day, asset.id)} />
    </article>)}
    {items.some(({ title }) => title) &&
      <p className="insight-muted">Place names from GeoNames (geonames.org), CC BY 4.0.</p>}
    <Paging {...cursor} next={result?.data?.next_cursor} forward={() => cursor.forward(result?.data?.next_cursor)} />
  </div>
}

function Stories(props: ScreenProps) {
  const { base, version, capability } = props
  const [day, setDay] = useState<string>()
  const cursor = usePageCursor('')
  const work = useAction(version)
  const result = useRemote<Page<StoryDay, string>>(day ? null : `${base}/insights/stories?${
    cursor.cursor ? `cursor=${cursor.cursor}` : ''}`, work.version)
  const items = result?.data?.items ?? []
  if (day) return <StoryDetail key={day} {...props} day={day} close={() => setDay(undefined)} />
  return <>
    <Status error={result?.error || capability.stages.story?.error}
      pending={!result || (!items.length && capability.stages.story?.pending)}
      empty={!items.length} retry={work.retry}>No day stories yet. Scan a library to create them.</Status>
    <div className="insight-list">
      {items.map((item) => <button className="insight-row" key={item.day} onClick={() => setDay(item.day)}>
        <strong>{item.day}</strong><span>{item.count} items · {item.segments} stops</span>
      </button>)}
    </div>
    <Paging {...cursor} next={result?.data?.next_cursor} forward={() => cursor.forward(result?.data?.next_cursor)} />
  </>
}

type Highlight = { start: number; end: number; score: number }
type Stage<T> = { status: 'pending' | 'done' | 'error'; error: string | null; data: T | null }
type VideoAnalysis = {
  override: boolean | null
  video: Stage<{ duration: number; highlights: Highlight[]; reasons: string[]
    preview: boolean; preview_error?: string; scene_changes: number }>
  transcript?: Stage<{ has_audio: boolean; segments: { start: number; end: number; text: string }[] }>
}
const deadReasons: Record<string, string> = {
  very_short: 'Very short', mostly_dark: 'Mostly dark', possible_pocket_recording: 'Possible pocket recording',
  low_detail_or_blur: 'Low detail or blur', static_and_quiet: 'Static and quiet',
}
function clock(seconds: number) {
  const whole = Math.floor(seconds)
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

function VideoDetail({ base, version, asset, enabled }: {
  base: string; version: number; asset: Asset; enabled: boolean
}) {
  const work = useAction(version)
  const url = `${base}/insights/assets/${asset.id}/video`
  const result = useRemote<VideoAnalysis>(url, work.version)
  const video = useRef<HTMLVideoElement>(null)
  const stop = useRef<number>(null)
  const [playError, setPlayError] = useState<string>()
  const analysis = result?.data?.video
  const transcript = result?.data?.transcript
  const reasons = analysis?.data?.reasons ?? []
  const flagged = result?.data?.override ?? reasons.length > 0
  function play(start: number, end?: number) {
    const player = video.current
    if (!player) return
    stop.current = end ?? null
    setPlayError(undefined)
    player.currentTime = start
    void player.play().catch((error: unknown) => setPlayError(message(error)))
  }
  function retry(stage: 'video' | 'transcript') {
    void work.action(`${base}/insights/assets/${asset.id}/${stage}/retry`, { method: 'POST' }).catch(() => {})
  }
  return <section className="insight-section" aria-label={assetName(asset)}>
    <h2>{assetName(asset)}</h2>
    <Status error={result?.error || work.error} pending={!result} retry={work.retry} />
    {analysis?.status === 'pending' && <p role="status" className="insight-muted">Video analysis queued.</p>}
    {analysis?.error && <Status error={analysis.error} retry={() => retry('video')} />}
    {analysis?.data?.preview && <video ref={video} className="insight-video" controls playsInline
      aria-label={assetName(asset)} preload="metadata" src={`${url}/preview?v=${asset.content_hash}`}
      onError={() => setPlayError('The video preview could not be played.')}
      onTimeUpdate={() => {
        if (video.current && stop.current !== null && video.current.currentTime >= stop.current) {
          video.current.pause()
          stop.current = null
        }
      }}>
      {!!transcript?.data?.segments.length &&
        <track kind="captions" label="Local transcript" srcLang="und" src={`${url}/captions`} />}
    </video>}
    <Status error={playError || analysis?.data?.preview_error}
      retry={analysis?.data?.preview_error || playError ? () => {
        setPlayError(undefined)
        video.current?.load()
        if (analysis?.data?.preview_error) retry('video')
      } : undefined} />
    {analysis?.data && <section>
      <h3>Highlights</h3>
      {analysis.data.highlights.length ? <div className="insight-actions">
        {analysis.data.highlights.map((highlight) => <button className="button secondary" key={highlight.start}
          disabled={!analysis.data?.preview} onClick={() => play(highlight.start, highlight.end)}>
          Play {clock(highlight.start)} – {clock(highlight.end)}
        </button>)}
      </div> : <p className="insight-muted">No highlight candidates.</p>}
    </section>}
    {result?.data && <section>
      <h3>Clip flag</h3>
      <p className="insight-muted" role="status">{flagged ? 'Flagged as a dead clip' : 'No dead-clip flag'}
        {reasons.length ? ` · ${reasons.map((reason) => deadReasons[reason] ?? reason).join(', ')}` : ''}</p>
      <div className="insight-actions">
        <button className="button secondary" disabled={work.busy}
          onClick={() => { void work.action(url, json('PATCH', { dead: !flagged })).catch(() => {}) }}>
          {flagged ? 'Clear dead-clip flag' : 'Flag as dead clip'}
        </button>
        {result.data.override !== null && <button className="button quiet" disabled={work.busy}
          onClick={() => { void work.action(url, json('PATCH', { dead: null })).catch(() => {}) }}>
          Restore automatic flag
        </button>}
      </div>
    </section>}
    <section>
      <h3>Transcript</h3>
      {!enabled && <p className="insight-muted">Local transcription is disabled.</p>}
      {enabled && transcript?.status === 'pending' && <p role="status">Transcription queued.</p>}
      {transcript?.error && <Status error={transcript.error} retry={() => retry('transcript')} />}
      {transcript?.data && (transcript.data.segments.length ? <ol className="transcript">
        {transcript.data.segments.map(({ start, end, text }, i) => <li key={`${start}-${i}`}>
          <button className="button quiet" disabled={!analysis?.data?.preview}
            onClick={() => play(start, end)}>{clock(start)}</button><span>{text}</span>
        </li>)}
      </ol> : <p>{transcript.data.has_audio ? 'No speech detected.' : 'This clip has no audio.'}</p>)}
    </section>
  </section>
}

function Videos({ base, version, capability, start }: ScreenProps & { start?: number }) {
  const cursor = usePageCursor(start ? start - 1 : 0)
  const work = useAction(version)
  const result = useRemote<Page<Asset>>(`${base}/assets?kind=video&limit=30&cursor=${cursor.cursor}`, work.version)
  const assets = result?.data?.items ?? []
  const [selected, setSelected] = useState<number>()
  const active = assets.find((item) => item.id === selected) ?? assets[0]
  const index = assets.findIndex((item) => item.id === active?.id)
  useShortcuts((key, event) => {
    if (event.repeat || event.ctrlKey || event.altKey || event.metaKey ||
        event.target instanceof HTMLVideoElement || !['arrowleft', 'arrowright'].includes(key)) return
    const target = assets[index + (key === 'arrowright' ? 1 : -1)]
    if (target) {
      event.preventDefault()
      setSelected(target.id)
    }
  })
  return <>
    <Status error={result?.error} pending={!result} empty={!assets.length} retry={work.retry}>
      No videos in this library.
    </Status>
    {active && <div className="video-layout">
      <div className="insight-list video-list" aria-label="Clips">
        {assets.map((asset) => <button className="insight-row" key={asset.id}
          aria-current={active.id === asset.id ? 'true' : undefined}
          onClick={() => setSelected(asset.id)}>{assetName(asset)}</button>)}
        <Paging {...cursor} next={result?.data?.next_cursor}
          forward={() => cursor.forward(result?.data?.next_cursor)} />
      </div>
      <VideoDetail key={active.id} base={base} version={version} asset={active} enabled={capability.transcript} />
    </div>}
  </>
}

const percent = (value: number) => `${Math.round(value * 100)}%`

function Recommendations({ base, capability, version, openReview }: ScreenProps) {
  const [minimum, setMinimum] = useState('0')
  const work = useAction(version)
  const state = capability.taste
  const enabled = state.status !== 'disabled'
  const result = useRemote<Page<Asset>>(enabled && state.status === 'ready'
    ? `${base}/assets?sort=taste&limit=12` : null, work.version)
  const assets = result?.data?.items ?? []
  return <>
    {capability.aesthetic && <section className="insight-section">
      <h2>Aesthetic score</h2>
      <div className="insight-actions">
        <label>Minimum score <select value={minimum} onChange={(event) => setMinimum(event.target.value)}>
          {[0, 0.25, 0.5, 0.65, 0.8].map((value) => <option key={value} value={value}>{percent(value)}</option>)}
        </select></label>
        <button className="button secondary" onClick={() =>
          openReview(`sort=aesthetic&min_aesthetic=${minimum}`, 'Aesthetic score')}>Review by aesthetic score</button>
      </div>
      {capability.stages.aesthetic?.pending && <p role="status" className="insight-muted">Scoring photos…</p>}
      <Status error={capability.stages.aesthetic?.error} />
    </section>}
    <section className="insight-section">
      <h2>Learned preferences</h2>
      {!enabled && <p className="insight-muted">Taste learning is disabled.</p>}
      {enabled && <>
        <p className="insight-muted">{state.positives} keep examples · {state.negatives} reject examples</p>
        {state.status === 'pending' && <p role="status">Training queued.</p>}
        {state.status === 'insufficient' && <p role="status">More rated photos are needed. Rate keepers 4–5 stars
          and rejects 1–2 stars or use the Reject flag.</p>}
        <Status error={state.error || work.error || result?.error} retry={work.retry} />
        <div className="insight-actions">
          <button className="button secondary" disabled={work.busy || state.status === 'pending'} onClick={() => {
            void work.action(`${base}/insights/taste/train`, { method: 'POST' }).catch(() => {})
          }}>Retrain from ratings</button>
          {state.status === 'ready' && <button className="button secondary"
            onClick={() => openReview('sort=taste', 'Recommendation score')}>Review recommendations</button>}
        </div>
        {state.status === 'ready' && !result?.error && <Status pending={!result} empty={!assets.length}>
          {capability.stages.taste?.pending ? 'Scoring photos…' : 'No recommendations available.'}
        </Status>}
        <div className="insight-grid">
          {assets.map((asset) => {
            const taste = asset.insights?.taste
            return <article className="insight-card" key={asset.id}>
              <button className="insight-image-button" onClick={() =>
                openReview(`day=${asset.day}`, asset.day, asset.id)}>
                <img src={mediaUrl(base, asset, 'thumb')} alt="" loading="lazy" />
                <span>{assetName(asset)}</span>
              </button>
              {taste?.score != null && <strong>{percent(taste.score)} recommendation</strong>}
              {taste?.factors && <ul className="taste-factors">
                {taste.factors.map(({ feature, contribution }) => <li key={feature}>
                  {feature} {contribution >= 0 ? '+' : '−'}{Math.abs(contribution).toFixed(2)}
                </li>)}
              </ul>}
            </article>
          })}
        </div>
      </>}
    </section>
  </>
}

type Transfer = {
  include_location?: boolean
  id: string; kind: 'copy' | 'zip' | 'report'; status: 'pending' | 'running' | 'cancelling' | 'cancelled' | 'error' | 'done'
  destination: string | null; error: string | null; processed: number; total: number
  total_bytes: number; written_bytes: number; cancellable: boolean
}
const bool = (value: FormDataEntryValue | null) => value === 'true' ? true : value === 'false' ? false : null

function FlagFilter({ name, label, initial = 'any' }: { name: string; label: string; initial?: string }) {
  return <label>{label}<select name={name} defaultValue={initial}>
    <option value="any">Any</option><option value="false">Exclude flagged</option>
    <option value="true">Only flagged</option>
  </select></label>
}

function Transfers({ base, version, capability, report }: ScreenProps & { report: boolean }) {
  const [includeLocation, setIncludeLocation] = useState(capability.strip_location === false)
  const [mode, setMode] = useState('zip')
  const [poll, setPoll] = useState(0)
  const [person, setPerson] = useState('')
  const work = useAction(version)
  const kind = report ? 'report' : 'export'
  const url = `${base}/insights/transfers`
  const result = useRemote<{ items: Transfer[] }>(`${url}?kind=${kind}`, work.version + poll)
  const cursor = usePageCursor(0)
  const people = useRemote<Page<Person>>(!report && capability.faces
    ? `${base}/insights/people?limit=100&cursor=${cursor.cursor}` : null, work.version)
  const tasks = result?.data?.items ?? []
  const running = tasks.some((item) => ['pending', 'running', 'cancelling'].includes(item.status))
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setPoll((v) => v + 1), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  return <>
    <form className="insight-form" onSubmit={(event) => {
      event.preventDefault()
      const data = new FormData(event.currentTarget)
      const dates = { start: data.get('start') || null, end: data.get('end') || null }
      const body = report ? dates : {
        kind: mode, destination: data.get('destination'), xmp: includeLocation && data.has('xmp'),
        include_location: includeLocation,
        selection: { ...dates, minimum_rating: Number(data.get('rating')), rejected: bool(data.get('rejected')),
          auto_reject: bool(data.get('auto_reject')), dead: bool(data.get('dead')),
          person_id: person ? Number(person) : null },
      }
      void work.action(`${base}/insights/${report ? 'reports' : 'exports'}`, json('POST', body))
        .catch(() => {})
    }}>
      {!report && <>
        <label>Format<select value={mode} onChange={(event) => setMode(event.target.value)}>
          <option value="zip">Download ZIP</option>
          <option value="copy" disabled={!capability.destinations.length}>Copy to an allowed folder</option>
        </select></label>
        {mode === 'copy' && <label>Destination<select name="destination" required>
          {capability.destinations.map((name) => <option key={name}>{name}</option>)}
        </select></label>}
        <label>Minimum rating<select name="rating" defaultValue={3}>
          {[0, 1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value} stars</option>)}
        </select></label>
        <FlagFilter name="rejected" label="Manual reject flag" initial="false" />
        <FlagFilter name="auto_reject" label="Auto-reject flag" />
        {capability.video && <FlagFilter name="dead" label="Dead-clip flag" />}
        {capability.faces && <>
          <label>Person<select value={person} onChange={(event) => setPerson(event.target.value)}>
            <option value="">Any person</option>
            {person && !people?.data?.items.some(({ id }) => String(id) === person) &&
              <option value={person}>Selected person {person}</option>}
            {people?.data?.items.map((item) =>
              <option key={item.id} value={item.id}>{personName(item)}</option>)}
          </select></label>
          <Paging {...cursor} next={people?.data?.next_cursor} forward={() => cursor.forward(people?.data?.next_cursor)} />
          <Status error={people?.error} pending={!people} retry={work.retry} />
        </>}
        <label>Location information<select value={String(includeLocation)}
          onChange={(event) => setIncludeLocation(event.target.value === 'true')}>
          <option value="false">Exclude location information</option>
          <option value="true">Include location information</option>
        </select></label>
        {includeLocation
          ? <label className="transfer-checkbox"><input type="checkbox" name="xmp" /> Include existing XMP sidecars</label>
          : <p className="insight-muted">Location-free exports use JPEG for stills and MP4 for video.</p>}
      </>}
      <label>From date<input type="date" name="start" /></label>
      <label>To date<input type="date" name="end" /></label>
      <button className="button primary" disabled={work.busy}>{report ? 'Generate HTML report' : 'Start export'}</button>
    </form>
    <Status error={work.error || result?.error} pending={!result} empty={!tasks.length} retry={work.retry}>
      {report ? 'No trip reports generated yet.' : 'No exports yet.'}
    </Status>
    <div className="insight-list">
      {tasks.map((item) => <article className="transfer-row" key={item.id}>
        <div><strong>{item.kind === 'report' ? 'Trip report' : item.kind === 'zip' ? 'Keeper ZIP' : 'Folder copy'}</strong>
          <p className="insight-muted"><span role="status">{item.status}</span> · {item.processed} / {item.total}
            {item.kind === 'report' ? ' days' : ' items'}</p>
          {item.status === 'done' && item.total === 0 &&
            <p className="insight-muted">{item.kind === 'report' ? 'No matching story days.' : 'No matching keepers.'}</p>}
          {item.include_location !== undefined && <p className="insight-muted">{item.include_location
            ? 'Source location included when available' : 'Location information excluded'}</p>}
          {item.kind === 'copy' && item.status === 'done' && <p className="copy-target">{item.destination}/{item.id}</p>}
          {item.error && <p role="alert" className="inline-error">{item.error}</p>}
          {['running', 'cancelling'].includes(item.status) && <progress aria-label="Transfer progress"
            max={item.total_bytes || item.total || 1}
            value={item.total_bytes ? item.written_bytes : item.total ? item.processed : undefined} />}
        </div>
        <div className="insight-actions">
          {item.status === 'done' && item.kind !== 'copy' && <a className="button secondary"
            href={`${url}/${item.id}/download`} download>Download {item.kind === 'report' ? 'HTML' : 'ZIP'}</a>}
          {item.cancellable && <button className="button secondary" disabled={work.busy}
            onClick={() => { void work.action(`${url}/${item.id}`, { method: 'DELETE' }).catch(() => {}) }}>Cancel</button>}
        </div>
      </article>)}
    </div>
  </>
}

export function InsightsScreen({ view, base, capability, version, openReview, error, retry, videoStart }: {
  view: Exclude<View, 'review' | 'archive'>; base: string; capability?: InsightCapabilities; version: number
  openReview: OpenReview; error?: string; retry: () => void; videoStart?: number
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { heading.current?.focus() }, [view])
  const label = views.find(({ id }) => id === view)?.label
  const enabled = capability && (view === 'taste'
    ? capability.aesthetic || capability.taste.status !== 'disabled' : capability[view])
  const props = capability && { base, version, capability, openReview }
  return <main className="insights-screen" aria-labelledby="insight-heading">
    <h1 id="insight-heading" tabIndex={-1} ref={heading}>{label}</h1>
    <Status error={error} pending={!capability} empty={!!capability && !enabled} retry={retry}>
      {label} are disabled in the library configuration.
    </Status>
    {enabled && props && (
      view === 'faces' ? <People {...props} /> :
      view === 'story' ? <Stories {...props} /> :
      view === 'video' ? <Videos {...props} start={videoStart} /> :
      view === 'taste' ? <Recommendations {...props} /> :
      <Transfers {...props} report={view === 'report'} />
    )}
  </main>
}
