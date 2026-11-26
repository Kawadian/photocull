import { Aperture, Images, Keyboard } from 'lucide-react'
import { useRef, useState } from 'react'
import { ApiError, assetName, json, message, request, useProgress, useRemote } from './api'
import type { Asset, Burst, GroupBy, Library, Rating } from './api'
import { ShortcutHelp } from './Dialog'
import { InsightsScreen, views } from './Insights'
import type { InsightCapabilities, View } from './Insights'
import { LibraryNav } from './LibraryNav'
import type { Lookup } from './ML'
import { Review } from './Review'
import { useShortcuts } from './shortcuts'

function Header({ connected, online, onHelp }: {
  connected?: boolean; online?: boolean; onHelp?: () => void
}) {
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
  const [view, setView] = useState<View>('review')
  const [insightFilter, setInsightFilter] = useState<{ params: string; label: string; assetId?: number }>()
  const [by, setBy] = useState<GroupBy>('folder')
  const [group, setGroup] = useState<string | null>(null)
  const [filters, setFilters] = useState('')
  const [tag, setTag] = useState('')
  const [lookup, setLookup] = useState<Lookup>()
  const [ratings, setRatings] = useState(new Map<number, Rating>())
  const [undo, setUndo] = useState<{ assetId: number; before: Rating; revision: number }>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [refresh, setRefresh] = useState(0)
  const [burstUndo, setBurstUndo] = useState<{ token: string; count: number }>()
  const [notice, setNotice] = useState<string>()
  const [help, setHelp] = useState(false)
  const inFlight = useRef(false)
  const searchInput = useRef<HTMLInputElement>(null)
  const focusBeforeSearch = useRef<HTMLElement>(null)
  const similarButton = useRef<HTMLButtonElement>(null)
  const reviewHeading = useRef<HTMLHeadingElement>(null)
  const version = (progress?.catalog_revision ?? 0) + refresh
  const capabilities = useRemote<InsightCapabilities>(`${base}/insights`, version)
  const reviewFilters = new URLSearchParams(insightFilter?.params ?? filters)
  if (tag && progress?.ml.tags_enabled) reviewFilters.set('tag', tag)

  function openReview(params: string, label: string, assetId?: number) {
    setInsightFilter({ params, label, assetId })
    setBy('folder')
    setGroup(null)
    setLookup(undefined)
    setTag('')
    setView('review')
    requestAnimationFrame(() => reviewHeading.current?.focus())
  }

  function selectTag(value: string) {
    setTag(value)
    setLookup(undefined)
    if (by === 'burst' || by === 'similar') {
      setBy('folder')
      setGroup(null)
    }
  }

  function closeLookup() {
    const fromSimilar = !!lookup?.source
    setLookup(undefined)
    window.requestAnimationFrame(() => {
      const target = fromSimilar ? similarButton.current : searchInput.current
      if (target && !target.disabled) target.focus()
      else reviewHeading.current?.focus()
    })
  }

  function escapeSearchInput() {
    if (lookup) {
      closeLookup()
      return true
    }
    const origin = focusBeforeSearch.current
    focusBeforeSearch.current = null
    if (origin?.isConnected) {
      origin.focus()
      return true
    }
    return false
  }

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
    if (key === 'escape' && view !== 'review') {
      event.preventDefault()
      setView('review')
      requestAnimationFrame(() => reviewHeading.current?.focus())
    }
    if (key === 'i') {
      event.preventDefault()
      setView(view === 'review' ? 'faces' : 'review')
    }
    if (view !== 'review' && key !== '?') return
    if (key === '/' && searchInput.current && !searchInput.current.disabled) {
      event.preventDefault()
      const active = document.activeElement
      focusBeforeSearch.current = active instanceof HTMLElement && active !== document.body
        ? active : reviewHeading.current
      searchInput.current.focus()
      searchInput.current.select()
    }
    if (key === 'escape' && lookup) {
      event.preventDefault()
      closeLookup()
    }
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
      <Header connected={connected} online={connected && progress?.worker_online} onHelp={() => setHelp(true)} />
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
      <nav className="insight-nav" aria-label="Library views">
        {views.map(({ id, label, icon: Icon }) => <button key={id}
          aria-current={view === id ? 'page' : undefined} onClick={() => setView(id)}>
          <Icon size={15} aria-hidden="true" /> {label}
        </button>)}
        {view !== 'review' && libraries.length > 1 && <label className="view-library">
          Library <select value={library} onChange={(event) => selectLibrary(event.target.value)}>
            {libraries.map(({ name }) => <option key={name}>{name}</option>)}
          </select>
        </label>}
      </nav>
      {view !== 'review' ? <InsightsScreen key={view} view={view} base={base}
        capability={capabilities?.data} version={version} openReview={openReview}
        error={capabilities?.error} retry={() => setRefresh((value) => value + 1)} /> : <>
      {insightFilter && <div className="status-banner" role="status">
        {insightFilter.label}
        <button className="button quiet" onClick={() => setInsightFilter(undefined)}>Clear insight filter</button>
      </div>}
      <div className="workspace">
        <LibraryNav
          base={base} libraries={libraries} library={library} selectLibrary={selectLibrary}
          by={by} selectBy={(mode) => {
            setLookup(undefined)
            setInsightFilter(undefined)
            if (by !== mode) {
              setBy(mode)
              setGroup(null)
              if (mode === 'burst') setTag('')
            }
          }}
          group={group} selectGroup={(value) => { setGroup(value); setLookup(undefined); setInsightFilter(undefined) }}
          version={version} progress={progress} browsing={!lookup}
          filters={filters} setFilters={(value) => {
            if (by === 'burst' && value !== filters) setGroup(null)
            setFilters(value)
            setInsightFilter(undefined)
            setLookup(undefined)
          }}
          tag={tag} selectTag={selectTag} onSearch={(query) => setLookup({
            title: `Search · ${query}`, url: `${base}/search`, body: { query, tag: tag || undefined },
          })}
          searchInputRef={searchInput} onSearchEscape={escapeSearchInput}
          watching={capabilities?.data?.watch}
          analysisBusy={busy} onClearAll={clearAll}
        />
        {!lookup && (by === 'burst' || by === 'similar') && group === null ? (
          <main className="image-stage empty"><div><Images size={30} />
            <h2>{by === 'burst' ? 'Select a burst to review it' : 'Select a group to review similar photos'}</h2>
            {by === 'burst' && <p>Use <kbd>[</kbd> and <kbd>]</kbd> to move between bursts.</p>}
          </div></main>
        ) : <Review
          key={JSON.stringify([by, group, by === 'burst' ? '' : String(reviewFilters), insightFilter?.assetId])}
          startAsset={insightFilter?.assetId}
          base={base} by={by} group={group} version={version} ratings={ratings}
          busy={busy} undoAvailable={!!undo} onRating={rate} onUndo={undoRating}
          filters={String(reviewFilters)} onChooseBest={chooseBest} onRejectRest={rejectRest}
          lookup={lookup} onCloseLookup={closeLookup} ml={progress?.ml}
          workerOnline={!!progress?.worker_online} headingRef={reviewHeading} similarRef={similarButton}
          onVideo={capabilities?.data?.video ? () => setView('video') : undefined}
          onTag={selectTag} activeTag={tag}
          onSimilar={(asset) => setLookup({
            title: `Similar to ${assetName(asset)}`,
            url: `${base}/assets/${asset.id}/similar`, body: { tag: tag || undefined }, source: asset,
          })}
          onClearFlag={(asset) => analysisAction(`assets/${asset.id}/auto-reject`, { method: 'DELETE' })}
        />}
      </div>
      </>}
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
