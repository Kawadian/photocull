import { Aperture, Images } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError, json, message, request, useProgress, useRemote } from './api'
import type { Asset, GroupBy, Library, Rating } from './api'
import { LibraryNav } from './LibraryNav'
import { Review } from './Review'

function Header({ connected, heartbeat }: { connected?: boolean; heartbeat?: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 5000)
    return () => window.clearInterval(timer)
  }, [])
  const online = connected && heartbeat !== undefined && now - heartbeat < 30

  return (
    <header className="app-header">
      <div className="wordmark"><Aperture size={24} strokeWidth={1.9} /><span>Photocull</span></div>
      {connected !== undefined && (
        <div className="worker-indicator" role="status">
          <span className={online ? 'online' : undefined} />
          {!connected ? 'Connecting to API' : online ? 'Worker online' : 'Worker offline'}
        </div>
      )}
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
  const [ratings, setRatings] = useState(new Map<number, Rating>())
  const [undo, setUndo] = useState<{ assetId: number; before: Rating; revision: number }>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [refresh, setRefresh] = useState(0)
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

  return (
    <div className="app">
      <Header connected={connected} heartbeat={progress?.worker?.heartbeat} />
      {error && (
        <div className="status-banner" role="alert">
          {error} <button className="button quiet" onClick={() => setError(undefined)}>Dismiss</button>
        </div>
      )}
      <div className="workspace">
        <LibraryNav
          base={base} libraries={libraries} library={library} selectLibrary={selectLibrary}
          by={by} selectBy={(mode) => { if (by !== mode) { setBy(mode); setGroup(null) } }}
          group={group} selectGroup={setGroup} version={version} progress={progress}
        />
        <Review
          key={JSON.stringify([by, group])}
          base={base} by={by} group={group} version={version} ratings={ratings}
          busy={busy} undoAvailable={!!undo} onRating={rate} onUndo={undoRating}
        />
      </div>
    </div>
  )
}

export function App() {
  const result = useRemote<{ items: Library[] }>('/api/libraries')
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
        {result && <p>{result.error || 'Set a library root in the Photocull configuration and restart the API.'}</p>}
      </main>
    </div>
  )
}
