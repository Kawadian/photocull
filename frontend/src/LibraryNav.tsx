import { CalendarDays, ChevronLeft, ChevronRight, Folder, Images, Layers, RefreshCw, Scan } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { Ref } from 'react'
import { AnalysisFilters, ConfirmAction } from './Analysis'
import { assetName, message, request, useRemote } from './api'
import type { Burst, Group, GroupBy, Library, Page, Progress, ScanError, SimilarGroup } from './api'
import { Dialog } from './Dialog'
import { PhotoTools } from './ML'
import { useShortcuts } from './shortcuts'

type NavProps = {
  watching?: boolean
  base: string
  libraries: Library[]
  library: string
  selectLibrary: (name: string) => void
  by: GroupBy
  selectBy: (by: GroupBy) => void
  group: string | null
  selectGroup: (group: string | null) => void
  version: number
  progress: Progress | undefined
  filters: string
  setFilters: (filters: string) => void
  analysisBusy: boolean
  onClearAll: () => Promise<void>
  browsing: boolean
  tag: string
  selectTag: (tag: string) => void
  onSearch: (query: string) => void
  searchInputRef: Ref<HTMLInputElement>
  onSearchEscape: () => boolean
}

function GroupList({ base, by, group, selectGroup, version, filters, browsing, progress }: Pick<NavProps,
  'base' | 'by' | 'group' | 'selectGroup' | 'version' | 'filters' | 'browsing' | 'progress'>) {
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [retry, setRetry] = useState(0)
  const pendingBoundary = useRef<'first' | 'last'>(null)
  const cursor = cursors.at(-1) ?? null
  const isBurst = by === 'burst'
  const isSimilar = by === 'similar'
  const grouped = isBurst || isSimilar
  const params = new URLSearchParams(isBurst ? filters : isSimilar ? '' : new URLSearchParams({ by }))
  if (cursor !== null) params.set('cursor', cursor)
  const groups = useRemote<Page<Group | Burst | SimilarGroup, string | number>>(
    `${base}/${isBurst ? 'bursts' : isSimilar ? 'similar-groups' : 'groups'}?${params}`, version + retry,
  )
  const items = groups?.data?.items
  const next = groups?.data?.next_cursor
  const Icon = isBurst ? Layers : isSimilar ? Scan : by === 'folder' ? Folder : CalendarDays

  useEffect(() => {
    if (!groups || pendingBoundary.current === null) return
    const item = items?.at(pendingBoundary.current === 'first' ? 0 : -1)
    pendingBoundary.current = null
    if (item && 'id' in item) selectGroup(String(item.id))
  }, [groups, items, selectGroup])

  useShortcuts((key, event) => {
    if (!browsing || !isBurst || !items || groups?.error || (key !== '[' && key !== ']') ||
      event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return
    event.preventDefault()
    const direction = key === '[' ? -1 : 1
    const index = items.findIndex((item) => 'id' in item && String(item.id) === group)
    const target = items[index < 0 ? (direction > 0 ? 0 : items.length - 1) : index + direction]
    if (target && 'id' in target) {
      selectGroup(String(target.id))
    } else if (direction < 0 && cursors.length > 1) {
      pendingBoundary.current = 'last'
      setCursors(cursors.slice(0, -1))
    } else if (direction > 0 && next != null) {
      pendingBoundary.current = 'first'
      setCursors([...cursors, String(next)])
    }
  })

  return (
    <>
      <div className="group-list">
        {!grouped && <button
          className="group-row"
          aria-current={browsing && group === null ? 'true' : undefined}
          onClick={() => selectGroup(null)}
        >
          <Images size={16} />
          <span>All assets</span>
        </button>}
        {!groups?.error && items?.map((item) => {
          const key = 'id' in item ? String(item.id) : item.name
          const name = 'path' in item ? assetName(item)
            : 'id' in item ? item.captured_at.replace('T', ' ') : item.name || 'Library root'
          return (
            <button
              key={key}
              className="group-row"
              aria-current={browsing && key === group ? 'true' : undefined}
              aria-label={'id' in item
                ? `${'path' in item ? 'Similar group' : 'Burst at'} ${name}, ${item.member_count} photos`
                : undefined}
              title={name}
              onClick={() => selectGroup(key)}
            >
              <Icon size={16} />
              <span>{name}</span>
              <small>{'id' in item ? item.member_count : item.count}</small>
            </button>
          )
        })}
        {!groups && <p className="group-empty" role="status">Loading {isBurst ? 'bursts' : 'groups'}…</p>}
        {isBurst && !groups?.error && items?.length === 0 && <p className="group-empty" role="status">
          {filters ? 'No bursts match these filters.' : cursors.length > 1 ? 'This page is empty.' : 'No bursts yet.'}
        </p>}
        {isSimilar && !groups?.error && items?.length === 0 && <p className="group-empty" role="status">
          {progress?.ml.grouping && !progress.ml.image_error && !progress.ml.group_error
            ? 'Finding similar photos…' : cursors.length > 1 ? 'This page is empty.' : 'No similar groups yet.'}
        </p>}
        {groups?.error && <div className="group-feedback">
          <p className="inline-error" role="alert">{groups.error}</p>
          <button className="button quiet" onClick={() => setRetry((value) => value + 1)}>Retry groups</button>
        </div>}
      </div>
      {(cursors.length > 1 || next != null) && (
        <div className="group-pages">
          <button
            className="icon-button"
            title="Previous groups"
            aria-label="Previous groups"
            disabled={cursors.length < 2}
            onClick={() => setCursors((values) => values.slice(0, -1))}
          ><ChevronLeft size={16} /></button>
          <span>Page {cursors.length}</span>
          <button
            className="icon-button"
            title="Next groups"
            aria-label="Next groups"
            disabled={next == null || !!groups?.error}
            onClick={() => next != null && setCursors((values) => [...values, String(next)])}
          ><ChevronRight size={16} /></button>
        </div>
      )}
    </>
  )
}

const readFailures: Record<ScanError['code'], string> = {
  permission_denied: 'Permission denied',
  missing: 'Disappeared during the scan',
  changed: 'Changed during the scan',
  io_error: 'File system error',
}

const scanOperations: Record<ScanError['operation'], string> = {
  list: 'Listing folder',
  stat: 'Checking path',
  read: 'Reading file',
}

function ScanErrors({ url, scan, onClose }: {
  url: string; scan: NonNullable<Progress['scan']>; onClose: () => void
}) {
  const [pages, setPages] = useState([0])
  const [retry, setRetry] = useState(0)
  const result = useRemote<Page<ScanError>>(
    `${url}?cursor=${pages.at(-1)}`, scan.file_errors + scan.directory_errors + retry,
  )
  const next = result?.data?.next_cursor
  return (
    <Dialog title="Scan errors" onClose={onClose}>
      <p>Previously indexed items are kept when their contents or parent folder cannot be read.</p>
      {!result && <p role="status">Loading scan errors…</p>}
      {result?.error && <div>
        <p className="inline-error" role="alert">{result.error}</p>
        <button className="button secondary" onClick={() => setRetry((value) => value + 1)}>Retry</button>
      </div>}
      {result?.data && (result.data.items.length
        ? <ul className="scan-errors">
            {result.data.items.map((issue) => <li key={issue.id}>
              <strong>{issue.path === '.' && issue.kind === 'directory' ? 'Library root' : issue.path}</strong>
              <span>{scanOperations[issue.operation]} · {readFailures[issue.code]}
                {issue.code === 'io_error' && issue.errno != null ? ` (errno ${issue.errno})` : ''}</span>
            </li>)}
          </ul>
        : <p>No scan errors on this page.</p>)}
      <div className="dialog-actions">
        <button className="button secondary" disabled={pages.length === 1}
          aria-label="Previous page of scan errors"
          onClick={() => setPages((values) => values.slice(0, -1))}>Previous</button>
        <button className="button secondary" disabled={next == null || !!result?.error}
          aria-label="Next page of scan errors"
          onClick={() => next != null && setPages((values) => [...values, next])}>Next</button>
        <button autoFocus className="button secondary" onClick={onClose}>Close</button>
      </div>
    </Dialog>
  )
}

function WorkerProgress({ base, progress }: { base: string; progress?: Progress }) {
  const [shownReport, setShownReport] = useState<string>()
  const reportButton = useRef<HTMLButtonElement>(null)
  const rows = progress?.stages
  const counts = rows ? Object.values(rows) : []
  const total = counts.reduce((sum, stage) =>
    sum + Object.values(stage).reduce((count, value) => count + value, 0), 0)
  const remaining = counts.reduce((sum, stage) => sum + (stage.pending ?? 0) + (stage.running ?? 0), 0)
  const errors = counts.reduce((sum, stage) => sum + (stage.error ?? 0), 0)
  const scan = progress?.scan
  const reportKey = scan && `${scan.id}:${scan.attempt}`
  const scanning = scan?.status === 'running' || scan?.status === 'pending'
  const failures = scan ? [
    scan.file_errors ? `${scan.file_errors.toLocaleString()} unreadable ${scan.file_errors === 1 ? 'file' : 'files'}` : '',
    scan.directory_errors ? `${scan.directory_errors.toLocaleString()} unreadable ${scan.directory_errors === 1 ? 'folder' : 'folders'}` : '',
  ].filter(Boolean).join(', ') : ''
  const ml = progress?.ml
  const embedding = rows?.embedding
  const embedded = embedding?.done ?? 0
  const embeddingTotal = Object.values(embedding ?? {}).reduce((sum, count) => sum + count, 0)

  return (
    <div className="worker-progress">
      <div className="section-heading">Indexing</div>
      <div className="progress-line">
        <span>{scan?.status === 'pending' ? 'Scan queued' : scanning ? 'Scanning library' : remaining ? 'Processing media' : 'Queue idle'}</span>
        {scanning && scan ? <span>{scan.files_indexed.toLocaleString()} indexed</span>
          : !!total && <span>{total - remaining} / {total}</span>}
      </div>
      <progress aria-label="Indexing progress" max={total || 1} value={scanning ? undefined : total - remaining} />
      {!!rows && <div className="progress-line stage-counts">
        <span>EXIF {rows.exif.done ?? 0}</span>
        <span>Previews {rows.preview.done ?? 0}</span>
        <span>Scores {rows.score.done ?? 0}</span>
      </div>}
      {ml?.enabled && <div className="embedding-progress">
        <div className="progress-line"><span>Photo embeddings</span>
          <span>{embedded} / {embeddingTotal}</span></div>
        <progress aria-label="Embedding progress" max={embeddingTotal || 1} value={embedded} />
        {!!embedding?.skipped && <p className="score-state">{embedding.skipped} skipped</p>}
        {ml.image_error && (!progress?.worker_online || !ml.search_enabled || !!embedded || ml.text_error) &&
          <p className="inline-error" role="status">{ml.image_error}</p>}
        {ml.text_error && (!progress?.worker_online || !ml.search_enabled) &&
          <p className="inline-error" role="status">{ml.text_error}</p>}
        {ml.tag_error && <p className="inline-error" role="status">{ml.tag_error}</p>}
        {ml.group_error && <p className="inline-error" role="status">{ml.group_error}</p>}
      </div>}
      {!!errors && <p className="inline-error">{errors} {errors === 1 ? 'stage' : 'stages'} failed</p>}
      {scan?.error && <p className="inline-error" title={scan.error}>{scan.error}</p>}
      {scan && failures && <>
        <button ref={reportButton} className="button secondary"
          onClick={() => setShownReport(reportKey ?? undefined)}>{failures}</button>
        {shownReport === reportKey && <ScanErrors url={`${base}/scans/${scan.id}/errors`}
          scan={scan} onClose={() => {
            setShownReport(undefined)
            requestAnimationFrame(() => reportButton.current?.focus())
          }} />}
      </>}
    </div>
  )
}

export function LibraryNav(props: NavProps) {
  const { base, libraries, library, selectLibrary, by, selectBy, progress, filters,
    setFilters, analysisBusy, onClearAll, tag, selectTag, onSearch, searchInputRef, onSearchEscape } = props
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string>()
  const [confirmClear, setConfirmClear] = useState(false)
  const scanActive = progress?.scan?.status === 'pending' || progress?.scan?.status === 'running'

  async function scan() {
    setScanning(true)
    setError(undefined)
    try {
      await request(`${base}/scans`, { method: 'POST' })
    } catch (failure) {
      setError(message(failure))
    } finally {
      setScanning(false)
    }
  }

  return (
    <aside className="library-nav" aria-label="Library navigation">
      <div className="library-top">
        <label className="section-heading" htmlFor="library">Library</label>
        <select id="library" value={library} onChange={(event) => selectLibrary(event.target.value)}>
          {libraries.map(({ name }) => <option key={name}>{name}</option>)}
        </select>
        <button className="button secondary scan" onClick={scan} disabled={scanning || scanActive}>
          <RefreshCw size={15} className={scanActive ? 'spin' : undefined} />
          {scanActive ? 'Scan queued or running' : 'Scan library'}
        </button>
        {error && <p className="inline-error" role="alert">{error}</p>}
      </div>
      {props.watching && <p className="watch-status">Folder watching on</p>}
      <PhotoTools base={base} progress={progress} tag={tag} selectTag={selectTag} onSearch={onSearch}
        inputRef={searchInputRef} onEscape={onSearchEscape} />
      <nav className="group-nav" aria-label="Browse assets">
        <div className="section-heading">Browse</div>
        <div className="segment" role="group" aria-label="Group by">
          <button aria-pressed={by === 'folder'} onClick={() => selectBy('folder')}>
            <Folder size={15} /> Folders
          </button>
          <button aria-pressed={by === 'day'} onClick={() => selectBy('day')}>
            <CalendarDays size={15} /> Days
          </button>
          <button aria-pressed={by === 'burst'} onClick={() => selectBy('burst')}>
            <Layers size={15} /> Bursts
          </button>
          {progress?.ml.enabled && (
            <button aria-pressed={by === 'similar'} onClick={() => selectBy('similar')}>
              <Scan size={15} /> Similar
            </button>
          )}
        </div>
        <GroupList key={JSON.stringify([by, by === 'burst' ? filters : ''])} {...props} />
      </nav>
      <AnalysisFilters active={!!filters} onChange={setFilters} />
      <button className="button quiet clear-all" disabled={analysisBusy}
        onClick={() => setConfirmClear(true)}>Clear all auto-rejects…</button>
      {confirmClear && <ConfirmAction
        title="Clear auto-rejects from this library?"
        description="This dismisses all current flags, including flags applied during burst review."
        action="Clear all flags"
        onConfirm={onClearAll}
        onClose={() => setConfirmClear(false)}
      />}
      <WorkerProgress key={base} base={base} progress={progress} />
    </aside>
  )
}
