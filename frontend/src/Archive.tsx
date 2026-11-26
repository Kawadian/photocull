import { useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { Dialog } from './Dialog'
import { Status } from './Insights'
import { json, message, request, useRemote } from './api'

const operations = {
  copy: 'Copy selected files', dng: 'Lossless DNG', jpeg: 'JPEG', heic: 'HEIC', avif: 'AVIF',
  hevc: 'HEVC video', av1: 'AV1 video', rename: 'Batch rename', trash: 'Move to trash',
  restore: 'Restore from trash', undo: 'Undo rename', purge: 'Permanently delete',
} as const
type Operation = keyof typeof operations
type Mode = 'export' | 'overwrite' | 'remove'
type Plan = {
  operation: Operation; mode?: Mode; filters?: Record<string, unknown>; trash_ids?: string[]
  collision?: 'fail' | 'rename'; preset?: string; quality?: number; template?: string; reason?: string
}
type FileRecord = { path: string; size: number; original?: string; sha256?: string }
type Preview = {
  confirmation: string; count: number; source_bytes: number; estimated_output_bytes: number
  estimated_reduction_bytes: number; estimated_after_purge_bytes: number; estimated_seconds: number
  next_id: number | null
  items: { relative: string; files: { source: FileRecord; target: string | null; remove: boolean }[] }[]
  skipped: { path: string; reason: string }[]
}
type Capability = { export: boolean; destructive: boolean; retention_days: number; destructive_reason?: string; export_reason?: string }
type Job = {
  id: string; actor: string; request: Plan; created_at: number; finished_at: number | null
  cancelled: number; counts: Record<string, number>; items?: { ordinal: number; path: string; status: string; phase?: string; error: string }[]
}
type Trash = { id: string; created_at: number; reason: string; files: FileRecord[]; renamed_to: boolean }
type Page<T, Cursor = string> = { items: T[]; next_cursor: Cursor | null }
type Audit = { id: number; at: number; actor: string; action: string; job_id: string; ordinal: number; details: object }
type Metrics = { count: number; bytes: number | null; estimated_bytes: number | null }
type Duplicate = { id: number; truncated: boolean; assets: { id: number; path: string; size: number; rating: number }[] }

function bytes(value: number | null | undefined): string {
  let amount = value ?? 0
  for (const unit of ['B', 'KiB', 'MiB', 'GiB', 'TiB']) {
    if (amount < 1024 || unit === 'TiB') {
      return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: unit === 'B' ? 0 : 1 }).format(amount)} ${unit}`
    }
    amount /= 1024
  }
  return ''
}

function seconds(value: number): string {
  if (value < 60) return `${Math.ceil(value)} s`
  if (value < 3600) return `${Math.ceil(value / 60)} min`
  return `${(value / 3600).toFixed(1)} h`
}

function timestamp(value: number) { return new Date(value * 1000).toLocaleString() }

function ReadState({ result, empty, text, retry }: {
  result?: { data?: unknown; error?: string }; empty?: boolean; text?: string; retry?: () => void
}) {
  return <Status pending={!result} error={result?.error} empty={!!result?.data && empty} retry={retry}>{text}</Status>
}

function More({ any, previous, next }: { any: boolean; previous: () => void; next?: () => void }) {
  if (!any && !next) return null
  return <div className="insight-actions" aria-label="Page controls">
    <button className="button secondary" disabled={!any} onClick={previous}>Previous page</button>
    <button className="button secondary" disabled={!next} onClick={next}>Next page</button>
  </div>
}

function ArchiveForm({ capability, seed, busy, execute, clear }: {
  capability: Capability; seed?: number[]; busy: boolean; execute: (plan: Plan) => void; clear: () => void
}) {
  const [operation, setOperation] = useState<Operation>('copy')
  const [mode, setMode] = useState<Mode>('export')
  const image = ['jpeg', 'heic', 'avif'].includes(operation)
  const video = operation === 'hevc' || operation === 'av1'
  const requiresWrite = operation === 'trash' || mode !== 'export'
  const requiresOutput = operation !== 'trash' && mode !== 'overwrite'

  function parsed(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget)
    const str = (key: string) => String(fields.get(key) ?? '').trim()
    const num = (key: string) => str(key) ? Number(str(key)) : undefined
    const list = (key: string) => str(key).split(/[ ,]+/).filter(Boolean)
    const maxRating = num('max_rating')
    execute({
      operation, mode, reason: str('reason'), collision: str('collision') === 'rename' ? 'rename' : 'fail',
      quality: num('quality'), preset: str('preset') || undefined, template: str('template') || undefined,
      filters: {
        ids: list('ids').map(Number), after_id: num('after_id'),
        start: str('start') || undefined, end: str('end') || undefined, older_days: num('older_days'),
        folder: str('folder'), camera: str('camera'), extensions: list('extensions').map((s) => s.replace(/^\./, '')),
        selection: str('selection'), burst_id: num('burst_id'), min_rating: num('min_rating'), max_rating: maxRating,
        min_width: num('min_width'), min_height: num('min_height'), min_bytes: num('min_bytes'),
        min_kbps: num('min_kbps'), codecs: list('codecs').map((s) => s.toLowerCase()),
      },
    })
  }

  return <form className="archive-form insight-form" onSubmit={parsed} onChange={clear}>
    <fieldset className="insight-fieldset" disabled={busy}>
      <legend>Operation</legend>
      <div className="archive-fields">
        <label>Action<select value={operation} onChange={(e) => { setOperation(e.target.value as Operation); setMode('export') }}>
          {Object.entries(operations).filter(([id]) => !['restore', 'purge', 'undo'].includes(id)).map(([id, name]) =>
            <option key={id} value={id} disabled={id === 'trash' && !capability.destructive}>{name}</option>)}
        </select></label>
        {!['copy', 'trash'].includes(operation) && <label>Destination<select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
          <option value="export">Separate export folder</option>
          <option value="overwrite" disabled={!capability.destructive}>Replace originals after verification</option>
          {operation !== 'rename' && <option value="remove" disabled={!capability.destructive}>Export and trash verified originals</option>}
        </select></label>}
        {operation !== 'trash' && <label>On name collision<select name="collision">
          <option value="fail">Skip conflicting file</option><option value="rename">Add a numeric suffix</option>
        </select></label>}
        {operation === 'rename' && <label>Filename template<input name="template" defaultValue="{date}_{stem}" maxLength={200} required
          aria-describedby="archive-template-help" /></label>}
        {(image || video) && <>
          <label>{video ? 'CRF (lower is higher quality)' : 'Quality (higher is higher quality)'}
            <input name="quality" type="number" min={video ? 0 : 1} max={operation === 'hevc' ? 51 : operation === 'av1' ? 63 : 100} placeholder="Codec default" />
          </label>
          <label>Encoder preset<select name="preset" defaultValue="balanced">
            <option value="fast">Fast</option><option value="balanced">Balanced</option><option value="slow">Slow</option>
          </select></label>
        </>}
        <label>Audit reason <input name="reason" maxLength={200} /></label>
      </div>
      {operation === 'rename' && <p id="archive-template-help" className="insight-muted">
        Tokens are {'{stem}, {date}, {year}, {camera}, {id}, {n}'}. The extension is kept.
      </p>}
    </fieldset>
    <fieldset className="insight-fieldset" disabled={busy}>
      <legend>Match files</legend>
      <div className="archive-fields">
        <label>Selection<select name="selection">
          <option value="all">All matching assets</option><option value="rejected">Manually rejected photos</option>
          <option value="suggested">Suggested rejects</option><option value="dead">Unwanted clips</option>
          <option value="burst_rest">Burst members except best</option>
        </select></label>
        <label>Asset IDs <input name="ids" defaultValue={seed?.join(', ')} placeholder="12, 56, 102" /></label>
        <label>Folder <input name="folder" placeholder="2018/Trip" /></label>
        <label>Camera <input name="camera" placeholder="ILCE-7CM2" /></label>
        <label>From date <input type="date" name="start" /></label>
        <label>Through date <input type="date" name="end" /></label>
        <label>At least this old (days) <input name="older_days" type="number" min="1" max="73000" /></label>
        <label>Extensions <input name="extensions" placeholder="arw, dng" /></label>
        <label>Minimum rating <input type="number" name="min_rating" min="0" max="5" /></label>
        <label>Maximum rating <input type="number" name="max_rating" min="0" max="5" /></label>
      </div>
      <details className="archive-advanced">
        <summary>Size, resolution and batch filters</summary>
        <div className="archive-fields">
          <label>Minimum bytes <input type="number" name="min_bytes" min="0" /></label>
          <label>Minimum width <input type="number" name="min_width" min="0" max="32768" /></label>
          <label>Minimum height <input type="number" name="min_height" min="0" max="32768" /></label>
          <label>Minimum video kbps <input type="number" name="min_kbps" min="0" /></label>
          <label>Source video codecs <input name="codecs" placeholder="h264, hevc" /></label>
          <label>Burst ID <input type="number" name="burst_id" min="1" /></label>
          <label>Start after asset ID <input type="number" name="after_id" min="1" /></label>
        </div>
      </details>
    </fieldset>
    {!capability.destructive && <p className="insight-muted">{capability.destructive_reason}</p>}
    {requiresOutput && !capability.export && <p className="insight-muted">{capability.export_reason}</p>}
    <button className="button primary archive-start" disabled={busy || requiresWrite && !capability.destructive || requiresOutput && !capability.export}>
      {busy ? 'Building preview…' : 'Preview operation'}
    </button>
  </form>
}

function PreviewDetails({ planned, proceed }: { planned: Preview; proceed: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { heading.current?.focus() }, [planned])
  return <section className="insight-section archive-preview" aria-label="Operation preview">
    <h3 ref={heading} tabIndex={-1}>Preview</h3>
    <div className="archive-metrics" role="status">
      <div><strong>{planned.count}</strong><span>Media items</span></div>
      <div><strong>{bytes(planned.source_bytes)}</strong><span>Source media</span></div>
      <div><strong>{bytes(planned.estimated_output_bytes)}</strong><span>Estimated output</span></div>
      <div><strong>{bytes(planned.estimated_reduction_bytes)}</strong><span>Estimated source size reduction</span></div>
      <div><strong>{bytes(planned.estimated_after_purge_bytes)}</strong><span>Estimated bytes freed after purge</span></div>
      <div><strong>{seconds(planned.estimated_seconds)}</strong><span>Estimated runtime</span></div>
    </div>
    <p className="insight-muted">Sizes and runtime are estimates. Originals in trash still use storage until purged.</p>
    {planned.next_id != null && <p>More matches are available. Use asset ID {planned.next_id} in the next batch.</p>}
    {!planned.count && <Status empty>No files are eligible for this operation.</Status>}
    {!!planned.items.length && <ul className="archive-list">
      {planned.items.map((item) => <li key={item.relative}>
        <strong>{item.relative}</strong>
        {item.files.map((file) => <div key={file.source.path}>
          {file.target || 'Trash or remove'} · {bytes(file.source.size)}
          {file.remove && file.target ? ' · original removed from its current location' : ''}
        </div>)}
      </li>)}
    </ul>}
    {!!planned.skipped.length && <details><summary>{planned.skipped.length} skipped</summary><ul className="archive-list">
      {planned.skipped.map((item) => <li key={item.path}><strong>{item.path}</strong><div>{item.reason}</div></li>)}
    </ul></details>}
    <div><button className="button primary" disabled={!planned.count} onClick={proceed}>Review and confirm</button></div>
  </section>
}

function TrashPanel({ url, capability, version, preview, busy, clear }: {
  url: string; capability: Capability; version: number; preview: (p: Plan) => void; busy: boolean; clear: () => void
}) {
  const [pages, setPages] = useState<string[]>([])
  const [checked, setChecked] = useState<string[]>([])
  const [collision, setCollision] = useState<'fail' | 'rename'>('fail')
  const result = useRemote<Page<Trash>>(`${url}/trash${pages.length ? `?before=${pages.at(-1)}` : ''}`, version)
  const selected = result?.data?.items.filter((item) => checked.includes(item.id)) ?? []
  function move(operation: 'restore' | 'undo' | 'purge') {
    preview({ operation, trash_ids: selected.map((item) => item.id), collision })
  }
  return <section className="insight-section" aria-label="Trash entries">
    <p className="insight-muted">{capability.retention_days ? `Automatic purge after ${capability.retention_days} days.` : 'Automatic purge is off.'}</p>
    {!capability.destructive && <p className="insight-muted">{capability.destructive_reason}</p>}
    <ReadState result={result} empty={!result?.data?.items.length} text="Trash is empty." />
    {!!result?.data?.items.length && <>
      <div className="insight-actions">
        <label>On restore collision<select value={collision} disabled={busy} onChange={(e) => { setCollision(e.target.value as 'fail' | 'rename'); clear() }}>
          <option value="fail">Skip conflicting entry</option><option value="rename">Add a numeric suffix</option>
        </select></label>
        <button className="button secondary" disabled={busy || !capability.destructive || !selected.length || selected.some((v) => v.renamed_to)} onClick={() => move('restore')}>Preview restore</button>
        <button className="button secondary" disabled={busy || !capability.destructive || !selected.length || selected.some((v) => !v.renamed_to)} onClick={() => move('undo')}>Preview undo rename</button>
        <button className="button secondary" disabled={busy || !capability.destructive || !selected.length} onClick={() => move('purge')}>Preview permanent deletion</button>
      </div>
      <ul className="archive-list">
        {result.data.items.map((item) => <li key={item.id}>
          <label className="archive-check"><input type="checkbox" disabled={busy} checked={checked.includes(item.id)} onChange={(e) => {
            setChecked((all) => e.target.checked ? [...all, item.id] : all.filter((id) => id !== item.id))
            clear()
          }} />{item.files[0]?.original}</label>
          <div>{timestamp(item.created_at)} · {bytes(item.files.reduce((total, file) => total + file.size, 0))} · {item.reason}</div>
          {item.renamed_to && <div>Rename can be undone</div>}
        </li>)}
      </ul>
    </>}
    <More any={!!pages.length} previous={() => { setPages((all) => all.slice(0, -1)); setChecked([]); clear() }}
      next={result?.data?.next_cursor ? () => { setPages((all) => [...all, result.data!.next_cursor!]); setChecked([]); clear() } : undefined} />
  </section>
}

function JobsPanel({ url, version, id, setId, action, busy }: {
  url: string; version: number; id?: string; setId: (id?: string) => void
  action: (path: string) => void; busy: boolean
}) {
  const result = useRemote<{ items: Job[] }>(`${url}/jobs`, version)
  const detailed = useRemote<Job>(id ? `${url}/jobs/${id}` : null, version)
  return <section className="insight-section" aria-label="Archive jobs">
    <ReadState result={result} empty={!result?.data?.items.length} text="No archive jobs yet." />
    {!!result?.data?.items.length && <ul className="archive-list archive-job-list">
      {result.data.items.map((job) => <li key={job.id}>
        <button className="button secondary" aria-pressed={id === job.id} onClick={() => setId(id === job.id ? undefined : job.id)}>
          {operations[job.request.operation]} · {timestamp(job.created_at)}
        </button>
        <div>{job.finished_at ? 'Finished' : job.cancelled ? 'Cancelling' : 'Queued or running'}</div>
        <div>{Object.entries(job.counts).map(([status, count]) => `${count} ${status}`).join(' · ')}</div>
      </li>)}
    </ul>}
    {id && <ReadState result={detailed} />}
    {detailed?.data && <JobDetails job={detailed.data} busy={busy} action={action} />}
  </section>
}

function JobDetails({ job, busy, action }: { job: Job; busy: boolean; action: (path: string) => void }) {
  const c = job.counts
  const total = Object.values(c).reduce((sum, value) => sum + value, 0)
  const processed = total - (c.pending ?? 0) - (c.running ?? 0)
  return <section className="insight-section" aria-label="Job details">
    <h3>{operations[job.request.operation]}</h3>
    <div className="insight-actions"><progress aria-label="Items processed" value={processed} max={total || 1} />
      <span>{processed} of {total} items processed</span></div>
    {!job.finished_at && <div><button className="button secondary" disabled={busy || !!job.cancelled}
      onClick={() => action(`jobs/${job.id}/cancel`)}>{job.cancelled ? 'Cancellation requested' : 'Cancel job'}</button></div>}
    {!!c.recovery && !!job.finished_at && <div><button className="button secondary" disabled={busy}
      onClick={() => action(`jobs/${job.id}/recover`)}>Retry blocked recovery</button></div>}
    <ul className="archive-list">{job.items?.map((item) => <li key={item.ordinal}>
      <strong>{item.path}</strong><div>{item.status}{item.phase ? ` · ${item.phase}` : ''}{item.error ? ` · ${item.error}` : ''}</div>
    </li>)}</ul>
  </section>
}

function StoragePanel({ url, version }: { url: string; version: number }) {
  const [by, setBy] = useState('year')
  const result = useRemote<{ total: Metrics; truncated: boolean; items: (Metrics & { dimension: string })[] }>(
    `${url}/storage?by=${by}`, version,
  )
  const data = result?.data
  const maximum = data?.items[0]?.bytes ?? 0
  return <section className="insight-section" aria-label="Storage analysis">
    <div className="insight-actions"><label>Group storage by<select value={by} onChange={(e) => setBy(e.target.value)}>
      <option value="year">Year</option><option value="folder">Folder</option><option value="format">Format</option><option value="camera">Camera</option>
    </select></label></div>
    <ReadState result={result} empty={!data?.items.length} text="There is no indexed media yet." />
    {!!data?.items.length && <>
      <div className="archive-metrics"><div><strong>{bytes(data.total.bytes)}</strong><span>Indexed storage · {data.total.count} assets</span></div>
        <div><strong>{bytes(data.total.estimated_bytes)}</strong><span>Estimated potential savings</span></div></div>
      <p className="insight-muted">Illustrative savings assume eligible originals can be replaced and purged. Preview a conversion to estimate a specific batch.</p>
      <ul className="archive-list">{data.items.map((row) => <li key={row.dimension}>
        <strong>{row.dimension || (by === 'folder' ? 'Library root' : 'Unknown')}</strong>
        <div>{bytes(row.bytes)} · {row.count} items · estimated savings {bytes(row.estimated_bytes)}</div>
        <meter aria-label={`Bytes in ${row.dimension || 'this group'}`} min={0} max={maximum || 1} value={row.bytes ?? 0} />
      </li>)}</ul>
      {data.truncated && <p className="insight-muted">Showing the 100 largest groups.</p>}
    </>}
  </section>
}

function DuplicatePanel({ url, version, selectIds }: { url: string; version: number; selectIds: (ids: number[]) => void }) {
  const [by, setBy] = useState('exact')
  const [pages, setPages] = useState<number[]>([])
  const [ids, setIds] = useState<number[]>([])
  const data = useRemote<Page<Duplicate, number>>(`${url}/duplicates?by=${by}&after=${pages.at(-1) ?? 0}`, version)
  return <section className="insight-section" aria-label="Duplicate detection">
    <div className="insight-actions"><label>Match<select value={by} onChange={(e) => { setBy(e.target.value); setPages([]); setIds([]) }}>
      <option value="exact">Same SHA-256</option><option value="near">Visually similar</option><option value="pairs">JPEG and RAW pairs</option>
    </select></label>
      <button className="button secondary" disabled={!ids.length} onClick={() => selectIds(ids)}>Use selected asset IDs</button>
    </div>
    {by !== 'exact' && <p className="insight-muted">Review each match before selecting files for an operation.</p>}
    <ReadState result={data} empty={!data?.data?.items.length} text="No matches in this view." />
    {data?.data?.items.map((group) => <fieldset className="insight-fieldset" key={group.id}>
      <legend>Group {group.id}</legend><ul className="archive-list">
        {group.assets.map((file) => <li key={file.id}><label className="archive-check"><input type="checkbox"
          checked={ids.includes(file.id)} onChange={(e) => setIds((all) => e.target.checked ? [...all, file.id] : all.filter((i) => i !== file.id))} />
        {file.path}</label><div>{bytes(file.size)} · rating {file.rating}</div></li>)}
      </ul>{group.truncated && <p className="insight-muted">Showing the first 100 assets in this group.</p>}</fieldset>)}
    <More any={!!pages.length} previous={() => { setPages((all) => all.slice(0, -1)); setIds([]) }}
      next={data?.data?.next_cursor ? () => { setPages((all) => [...all, data.data!.next_cursor!]); setIds([]) } : undefined} />
  </section>
}

function AuditPanel({ url, version }: { url: string; version: number }) {
  const [pages, setPages] = useState<number[]>([])
  const result = useRemote<Page<Audit, number>>(`${url}/audit?after=${pages.at(-1) ?? 0}`, version)
  return <section className="insight-section" aria-label="Audit log">
    <ReadState result={result} empty={!result?.data?.items.length} text="The audit log is empty." />
    <ul className="archive-list">{result?.data?.items.map((entry) => <li key={entry.id}>
      <details><summary>{entry.action} · {timestamp(entry.at)} · {entry.actor}</summary>
        <pre>{JSON.stringify({ job: entry.job_id, item: entry.ordinal, ...entry.details }, null, 2)}</pre>
      </details></li>)}</ul>
    <More any={!!pages.length} previous={() => setPages((all) => all.slice(0, -1))}
      next={result?.data?.next_cursor ? () => setPages((all) => [...all, result.data!.next_cursor!]) : undefined} />
  </section>
}

const panels = ['Plan', 'Jobs', 'Trash', 'Storage', 'Duplicates', 'Audit'] as const

export function Archive({ base, online }: { base: string; online: boolean }) {
  const url = `${base}/archive`
  const [panel, setPanel] = useState<typeof panels[number]>('Plan')
  const [version, setVersion] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [seed, setSeed] = useState<number[]>()
  const [planned, setPlanned] = useState<{ plan: Plan; result: Preview }>()
  const [confirm, setConfirm] = useState(false)
  const [jobId, setJobId] = useState<string>()
  const capability = useRemote<Capability>(url, version)
  const refresh = () => setVersion((n) => n + 1)

  useEffect(() => {
    if (!['Jobs', 'Trash', 'Audit'].includes(panel)) return
    const timer = window.setInterval(() => setVersion((n) => n + 1), 2000)
    return () => window.clearInterval(timer)
  }, [panel])

  async function mutate(path: string, body: object = {}) {
    setError('')
    setBusy(true)
    try {
      return (await request<{ id: string }>(`${url}/${path}`, json('POST', body))).id
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
      refresh()
    }
  }

  async function preview(plan: Plan) {
    setError('')
    setPlanned(undefined)
    setBusy(true)
    try {
      const result = await request<Preview>(`${url}/preview`, json('POST', plan))
      setPlanned({ plan, result })
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
    }
  }

  async function apply() {
    if (!planned) return
    const id = await mutate('jobs', { plan: planned.plan, confirmation: planned.result.confirmation })
    if (id) {
      setJobId(id)
      setPanel('Jobs')
      setPlanned(undefined)
      setConfirm(false)
    }
  }

  const cfg = capability?.data
  const screens: Record<typeof panels[number], ReactNode> = {
    Plan: cfg && <ArchiveForm capability={cfg} seed={seed} busy={busy} execute={preview} clear={() => setPlanned(undefined)} />,
    Jobs: <JobsPanel url={url} version={version} id={jobId} setId={setJobId} action={mutate} busy={busy} />,
    Trash: cfg && <TrashPanel url={url} capability={cfg} version={version} preview={preview} busy={busy} clear={() => setPlanned(undefined)} />,
    Storage: <StoragePanel url={url} version={version} />,
    Duplicates: <DuplicatePanel url={url} version={version} selectIds={(ids) => { setSeed(ids); setPanel('Plan') }} />,
    Audit: <AuditPanel url={url} version={version} />,
  }
  return <main className="insights archive" aria-labelledby="archive-heading">
    <h2 id="archive-heading">Archive</h2>
    <nav className="insight-actions" aria-label="Archive sections">
      {panels.map((name) => <button key={name} className="button secondary" aria-current={panel === name ? 'page' : undefined}
        disabled={busy} onClick={() => { setError(''); setPlanned(undefined); setPanel(name) }}>{name}</button>)}
    </nav>
    <ReadState result={capability} retry={refresh} />
    {!online && <p className="insight-muted">The worker is offline. Confirmed jobs will wait in the queue.</p>}
    {error && !confirm && <Status error={error} />}
    {screens[panel]}
    {planned && <PreviewDetails planned={planned.result} proceed={() => { setError(''); setConfirm(true) }} />}
    {confirm && planned && <Dialog title={operations[planned.plan.operation]} busy={busy} onClose={() => setConfirm(false)}>
      <p>{planned.result.count} {planned.result.count === 1 ? 'item' : 'items'} · {bytes(planned.result.source_bytes)} of source media.</p>
      <p>Estimated bytes freed after purge: {bytes(planned.result.estimated_after_purge_bytes)}.</p>
      {planned.plan.operation === 'purge' ? <p>Permanent deletion cannot be undone.</p>
        : planned.plan.mode && planned.plan.mode !== 'export' || planned.plan.operation === 'trash'
          ? <p>Originals will be moved to trash after verification.</p> : null}
      {error && <Status error={error} />}
      <div className="dialog-actions">
        <button autoFocus className="button secondary" disabled={busy} onClick={() => setConfirm(false)}>Go back</button>
        <button className="button primary" disabled={busy} onClick={apply}>{busy ? 'Queueing…' : 'Confirm and queue'}</button>
      </div>
    </Dialog>}
  </main>
}
