import { Aperture, Images, Keyboard } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError, json, message, request, useProgress, useRemote } from './api'
import type { Asset, Burst, GroupBy, Library, Rating } from './api'
import { ShortcutHelp } from './Dialog'
import { LibraryNav } from './LibraryNav'
import { Review } from './Review'
import { useShortcuts } from './shortcuts'

function Header({ connected, heartbeat, onHelp }: {
  connected?: boolean; heartbeat?: number; onHelp?: () => void
}) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 5000)
    return () => window.clearInterval(timer)
  }, [])
  const online = connected && heartbeat !== undefined && now - heartbeat < 30

  return (
    <header className="app-header">
      <div className="wordmark"><Aperture size={24} strokeWidth={1.9} /><span>Photocull</span></div>
      <div className="header-actions">
        {connected !== undefined && (
          <div className="worker-indicator" role="status">
            <span className={online ? 'online' : undefined} />
            {!connected ? 'Connecting to API' : online ? 'Worker online' : 'Worker offline'}
          </div>
        )}
        {onHelp && <button className="icon-button" aria-label="Keyboard shortcuts"
          aria-keyshortcuts="?" title="Keyboard shortcuts (?)" onClick={onHelp}><Keyboard size={17} /></button>}
      </div>
    </header>
  )
}

function Workspace({ libraries, library, selectLibrary }: {
  libraries: Library[]
  library: string
  selectLibrary: (library: string) => void
}) {
  const base = `/api/libraries/${library}`
  const { progress, connected } = useProgress(base)
  const [by, setBy] = useState<GroupBy>('folder')
  const [group, setGroup] = useState<string | null>(null)
  const [filters, setFilters] = useState('')
  const [ratings, setRatings] = useState(new Map<number, Rating>())
  const [undo, setUndo] = useState<{ assetId: number; before: Rating; revision: number }>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [refresh, setRefresh] = useState(0)
  const [burstUndo, setBurstUndo] = useState<{ token: string; count: number }>()
  const [notice, setNotice] = useState<string>()
  const [help, setHelp] = useState(false)
  const inFlight = useRef(false)
  const version = (progress?.catalog_revision ?? 0) + refresh

  async function changeRating(assetId: number, rating: number, rejected: boolean, revision: number) {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(undefined)
    try {
      const result = await request<Rating>(`${base}/assets/${assetId}/rating`, json('PATCH', {
        rating, rejected, expected_revision: revision,
      }))
      setRatings((values) => new Map(values).set(assetId, result))
      return result
    } catch (failure) {
      setError(message(failure))
      if (failure instanceof ApiError && failure.status === 409) {
        setUndo(undefined)
        setRefresh((value) => value + 1)
      }
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  async function rate(asset: Asset, rating: number, rejected = false) {
    if (asset.rating === rating && asset.rejected === rejected) return
    const result = await changeRating(asset.id, rating, rejected, asset.revision)
    if (result) {
      const before = { rating: asset.rating, rejected: asset.rejected, revision: asset.revision }
      setUndo({ assetId: asset.id, before, revision: result.revision })
    }
  }

  async function undoRating() {
    if (!undo) return
    const result = await changeRating(undo.assetId, undo.before.rating, undo.before.rejected, undo.revision)
    if (result) setUndo(undefined)
  }

  async function analysisAction<T>(path: string, init: RequestInit) {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(undefined)
    setNotice(undefined)
    try {
      return await request<T>(`${base}/${path}`, init)
    } catch (failure) {
      setError(message(failure))
    } finally {
      setRefresh((value) => value + 1)
      inFlight.current = false
      setBusy(false)
    }
  }

  async function chooseBest(burst: Burst, assetId: number) {
    await analysisAction<Burst>(`bursts/${burst.id}/best`, json('PATCH', {
      asset_id: assetId, membership_key: burst.membership_key,
    }))
  }

  async function rejectRest(burst: Burst) {
    const result = await analysisAction<{ undo_token: string | null; changed: number }>(
      `bursts/${burst.id}/reject-rest`, json('POST', {
        best_id: burst.best_id, membership_key: burst.membership_key,
      }),
    )
    if (result?.undo_token) setBurstUndo({ token: result.undo_token, count: result.changed })
    else if (result) setNotice('The remaining photos are already flagged.')
  }

  async function undoBurstRejection() {
    if (!burstUndo) return
    const result = await analysisAction<{ changed: number }>(
      `burst-rejections/${burstUndo.token}`, { method: 'DELETE' },
    )
    if (result) {
      setBurstUndo(undefined)
      setNotice(`Removed ${result.changed} burst flags.`)
    }
  }

  async function clearAll() {
    const result = await analysisAction<{ changed: number }>('auto-reject', { method: 'DELETE' })
    if (result) {
      setBurstUndo(undefined)
      setNotice(`Cleared ${result.changed} auto-reject flags.`)
    }
  }

  useShortcuts((key, event) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return
    if (key === '?') {
      event.preventDefault()
      setHelp(true)
    }
    if (key === 'u' && event.shiftKey && burstUndo && !busy) {
      event.preventDefault()
      void undoBurstRejection()
    }
  })

  return (
    <div className="app">
      <Header connected={connected} heartbeat={progress?.worker?.heartbeat} onHelp={() => setHelp(true)} />
      {help && <ShortcutHelp onClose={() => setHelp(false)} />}
      {error && (
        <div className="status-banner" role="alert">
          {error} <button className="button quiet" onClick={() => setError(undefined)}>Dismiss</button>
        </div>
      )}
      {notice && (
        <div className="status-banner" role="status">
          {notice} <button className="button quiet" onClick={() => setNotice(undefined)}>Dismiss</button>
        </div>
      )}
      {burstUndo && (
        <div className="status-banner" role="status">
          Flagged {burstUndo.count} {burstUndo.count === 1 ? 'photo' : 'photos'} in the burst.
          <button className="button quiet" aria-keyshortcuts="Shift+U" disabled={busy}
            onClick={undoBurstRejection}>Undo burst reject <kbd aria-hidden="true">⇧U</kbd></button>
        </div>
      )}
      <div className="workspace">
        <LibraryNav
          base={base} libraries={libraries} library={library} selectLibrary={selectLibrary}
          by={by} selectBy={(mode) => { if (by !== mode) { setBy(mode); setGroup(null) } }}
          group={group} selectGroup={setGroup} version={version} progress={progress}
          filters={filters} setFilters={(value) => {
            if (by === 'burst' && value !== filters) setGroup(null)
            setFilters(value)
          }}
          analysisBusy={busy} onClearAll={clearAll}
        />
        {by === 'burst' && group === null ? (
          <main className="image-stage empty"><div><Images size={30} /><h2>Select a burst to review it</h2>
            <p>Use <kbd>[</kbd> and <kbd>]</kbd> to move between bursts.</p>
          </div></main>
        ) : <Review
          key={JSON.stringify([by, group, by === 'burst' ? '' : filters])}
          base={base} by={by} group={group} version={version} ratings={ratings}
          busy={busy} undoAvailable={!!undo} onRating={rate} onUndo={undoRating}
          filters={filters} onChooseBest={chooseBest} onRejectRest={rejectRest}
          onClearFlag={(asset) => analysisAction(`assets/${asset.id}/auto-reject`, { method: 'DELETE' })}
        />}
      </div>
    </div>
  )
}

export function App() {
  const [retry, setRetry] = useState(0)
  const result = useRemote<{ items: Library[] }>('/api/libraries', retry)
  const [chosen, setChosen] = useState<string>()
  const libraries = result?.data?.items ?? []
  const active = libraries.find(({ name }) => name === chosen)?.name ?? libraries[0]?.name

  if (active) {
    return <Workspace key={active} libraries={libraries} library={active} selectLibrary={setChosen} />
  }
  return (
    <div className="app">
      <Header />
      <main className="initial-state">
        <Images size={36} />
        <h1>{!result ? 'Loading libraries…' : result.error ? 'Could not load libraries' : 'No libraries configured'}</h1>
        {result?.error ? <>
          <p role="alert">{result.error}</p>
          <button className="button secondary" onClick={() => setRetry((value) => value + 1)}>Retry</button>
        </> : result && <p>Set a library root in the Photocull configuration and restart the API.</p>}
      </main>
    </div>
  )
}
