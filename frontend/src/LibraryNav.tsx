import { CalendarDays, ChevronLeft, ChevronRight, Folder, Images, Layers, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { AnalysisFilters, ConfirmAction } from './Analysis'
import { message, request, useRemote } from './api'
import type { Burst, Group, GroupBy, Library, Page, Progress } from './api'

type NavProps = {
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
}

function GroupList({ base, by, group, selectGroup, version, filters }: Pick<NavProps,
  'base' | 'by' | 'group' | 'selectGroup' | 'version' | 'filters'>) {
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const cursor = cursors.at(-1) ?? null
  const isBurst = by === 'burst'
  const params = new URLSearchParams(isBurst ? filters : new URLSearchParams({ by }))
  if (cursor !== null) params.set('cursor', cursor)
  const groups = useRemote<Page<Group | Burst, string | number>>(
    `${base}/${isBurst ? 'bursts' : 'groups'}?${params}`, version,
  )
  const next = groups?.data?.next_cursor
  const Icon = isBurst ? Layers : by === 'folder' ? Folder : CalendarDays

  return (
    <>
      <div className="group-list">
        {!isBurst && <button
          className="group-row"
          aria-current={group === null ? 'true' : undefined}
          onClick={() => selectGroup(null)}
        >
          <Images size={16} />
          <span>All assets</span>
        </button>}
        {groups?.data?.items.map((item) => {
          const key = 'id' in item ? String(item.id) : item.name
          const name = 'id' in item ? item.captured_at.replace('T', ' ') : item.name || 'Library root'
          return (
            <button
              key={key}
              className="group-row"
              aria-current={key === group ? 'true' : undefined}
              title={name}
              onClick={() => selectGroup(key)}
            >
              <Icon size={16} />
              <span>{name}</span>
              <small>{'id' in item ? item.member_count : item.count}</small>
            </button>
          )
        })}
        {isBurst && groups?.data?.items.length === 0 && <p className="group-empty">No bursts on this page</p>}
        {groups?.error && <p className="inline-error">{groups.error}</p>}
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
            disabled={next == null}
            onClick={() => next != null && setCursors((values) => [...values, String(next)])}
          ><ChevronRight size={16} /></button>
        </div>
      )}
    </>
  )
}

export function WorkerProgress({ progress }: { progress?: Progress }) {
  const rows = progress?.stages
  const counts = rows ? Object.values(rows) : []
  const total = counts.reduce((sum, stage) =>
    sum + Object.values(stage).reduce((count, value) => count + value, 0), 0)
  const remaining = counts.reduce((sum, stage) => sum + (stage.pending ?? 0) + (stage.running ?? 0), 0)
  const errors = counts.reduce((sum, stage) => sum + (stage.error ?? 0), 0)
  const scanning = progress?.scan?.status === 'running' || progress?.scan?.status === 'pending'

  return (
    <div className="worker-progress">
      <div className="section-heading">Indexing</div>
      <div className="progress-line">
        <span>{scanning ? 'Scanning library' : remaining ? 'Processing media' : 'Queue idle'}</span>
        {!!total && <span>{total - remaining} / {total}</span>}
      </div>
      <progress aria-label="Indexing progress" max={total || 1} value={total - remaining} />
      {!!rows && <div className="progress-line stage-counts">
        <span>EXIF {rows.exif.done ?? 0}</span>
        <span>Previews {rows.preview.done ?? 0}</span>
        <span>Scores {rows.score.done ?? 0}</span>
      </div>}
      {!!errors && <p className="inline-error">{errors} {errors === 1 ? 'stage' : 'stages'} failed</p>}
      {progress?.scan?.error && <p className="inline-error" title={progress.scan.error}>{progress.scan.error}</p>}
    </div>
  )
}

export function LibraryNav(props: NavProps) {
  const { base, libraries, library, selectLibrary, by, selectBy, progress, filters,
    setFilters, analysisBusy, onClearAll } = props
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
      <nav className="group-nav" aria-label="Browse assets">
        <div className="section-heading">Browse</div>
        <div className="segment" aria-label="Group by">
          <button aria-pressed={by === 'folder'} onClick={() => selectBy('folder')}>
            <Folder size={15} /> Folders
          </button>
          <button aria-pressed={by === 'day'} onClick={() => selectBy('day')}>
            <CalendarDays size={15} /> Days
          </button>
          <button aria-pressed={by === 'burst'} onClick={() => selectBy('burst')}>
            <Layers size={15} /> Bursts
          </button>
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
      <WorkerProgress progress={progress} />
    </aside>
  )
}
