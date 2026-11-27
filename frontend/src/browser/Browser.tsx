import { Aperture, CalendarDays, CheckSquare, Folder, FolderPlus, Grid2X2, Images, ListFilter, Pencil, RefreshCw, SlidersHorizontal } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useRemote } from '../api'
import type { Library } from '../api'
import { AlbumEditor, Albums, DeleteAlbum, SelectionActions } from './Collections'
import { albumFilters, albumUrl, collectionText, maxSelection, saveAlbum } from './collections'
import type { Album, AssetRef } from './collections'
import { FiltersDialog, filterText } from './Filters'
import type { FilterValues } from './filterValues'
import { TimelineGrid } from './Grid'
import { densities, readLocation, writeLocation } from './location'
import type { BrowserLocation, Density, Language } from './location'
import { Preview } from './Preview'
import { Scrubber } from './Scrubber'
import { formatDate, strings } from './strings'
import { assetKey, browseUrl, useTimeline } from './timeline'
import type { TimelineAsset } from './timeline'
import './browser.css'

type Navigate = (updates: Partial<BrowserLocation>, replace?: boolean) => void

function Timeline({ location, months, navigate, jump, close, album, openAlbum, refreshAlbums }: {
  location: BrowserLocation
  months: string[]
  navigate: Navigate
  jump: (month: string) => void
  close: () => void
  album?: Album
  openAlbum: (album: Album) => void
  refreshAlbums: () => void
}) {
  const text = strings[location.lang]
  const filters = filterText[location.lang]
  const collection = collectionText[location.lang]
  const [selecting, setSelecting] = useState(false)
  const [selected, setSelected] = useState(new Map<string, AssetRef>())
  const [anchor, setAnchor] = useState('')
  const [selectionError, setSelectionError] = useState('')
  const [batchOpen, setBatchOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const selectionButton = useRef<HTMLButtonElement>(null)
  const actionsButton = useRef<HTMLButtonElement>(null)
  const feed = useTimeline(location)
  const media = feed.media
  const { next: cursor, busy, error, loadMore } = feed
  const loaded = feed.assets.length
  const [currentMonth, setCurrentMonth] = useState('')
  const opener = useRef<HTMLButtonElement>(null)
  const refreshLabels = useRef(false)
  const { reload, setFavorite } = feed
  const index = location.photo ? feed.assets.findIndex((asset) => assetKey(asset) === location.photo) : -1
  const openPhoto = (offset: number) => navigate({ photo: assetKey(feed.assets[index + offset]) }, true)
  const neighbors: TimelineAsset[] = []
  if (index > 0) neighbors.push(feed.assets[index - 1])
  if (index >= 0 && index < feed.assets.length - 1) neighbors.push(feed.assets[index + 1])

  function advance(random: boolean) {
    const stills = feed.assets.filter((asset) => asset.kind !== 'video' && assetKey(asset) !== location.photo)
    if (!stills.length) return false
    const destination = random ? stills[Math.floor(Math.random() * stills.length)]
      : feed.assets.slice(index + 1).find((asset) => asset.kind !== 'video') ?? stills[0]
    navigate({ photo: assetKey(destination) }, true)
    return true
  }

  useEffect(() => {
    if (location.photo) return
    opener.current?.focus({ preventScroll: true })
    opener.current = null
    if (refreshLabels.current) {
      reload()
      refreshLabels.current = false
    }
  }, [location.photo, reload])

  useEffect(() => {
    if (index >= 0 && index >= loaded - 5 && cursor && !error && !busy) loadMore()
  }, [index, loaded, cursor, error, busy, loadMore])

  function choose(itemIndex: number, range: boolean) {
    const choice = feed.assets[itemIndex]
    const key = assetKey(choice)
    const anchorIndex = range ? feed.assets.findIndex((item) => assetKey(item) === anchor) : -1
    const chosen = new Map(selected)
    if (anchorIndex !== -1) {
      for (const item of feed.assets.slice(Math.min(anchorIndex, itemIndex), Math.max(anchorIndex, itemIndex) + 1)) {
        chosen.set(assetKey(item), { id: item.id, library: item.library })
        if (chosen.size > maxSelection) break
      }
    } else {
      if (chosen.has(key)) chosen.delete(key)
      else chosen.set(key, { id: choice.id, library: choice.library })
      setAnchor(key)
    }
    setSelecting(true)
    setNotice('')
    if (chosen.size > maxSelection) setSelectionError(collection.limit)
    else {
      setSelected(chosen)
      setSelectionError('')
    }
  }

  function closeBatch() {
    setBatchOpen(false)
    requestAnimationFrame(() => (actionsButton.current?.disabled ? selectionButton : actionsButton).current?.focus())
  }

  return <>
    <div className="pcb-selection">
      <button className="pcb-control" type="button" ref={selectionButton} aria-pressed={selecting}
        onClick={() => {
          setSelecting(!selecting)
          setSelected(new Map())
          setAnchor('')
          setSelectionError('')
          setNotice('')
        }}><CheckSquare size={16} aria-hidden="true" />{selecting ? collection.done : collection.select}</button>
      {selecting && <>
        <span role="status" title={collection.selectHelp}>{collection.selection(selected.size)}</span>
        <button className="pcb-control pcb-apply" type="button" ref={actionsButton} aria-haspopup="dialog"
          disabled={!selected.size} onClick={() => setBatchOpen(true)}>{collection.actions}</button>
      </>}
      {selectionError && <span role="alert">{selectionError}</span>}
      {notice && <span role="status">{notice}</span>}
    </div>
    <div className="pcb-content">
    <TimelineGrid assets={feed.assets} location={location} recipe={media?.recipe} text={text} busy={feed.busy}
      canLoad={!!feed.next && !feed.error} loadMore={feed.loadMore} onMonth={setCurrentMonth}
      selection={{ active: selecting, keys: new Set(selected.keys()), choose }}
      onPhoto={(asset, button) => {
        opener.current = button
        navigate({ photo: assetKey(asset) })
      }}>
      {feed.error ? <div className="pcb-state" role="alert">
        <Images size={25} aria-hidden="true" />
        <p>{text.unavailable}</p>
        <button type="button" className="pcb-control" onClick={feed.loadMore}>{text.retry}</button>
        {location.library && <button type="button" className="pcb-control"
          onClick={() => navigate({ library: '', month: '' })}>{text.clearLibrary}</button>}
      </div> : feed.busy ? <div className={'pcb-state' + (feed.assets.length ? ' pcb-page-status' : '')} role="status">
        <span className="pcb-loading" aria-hidden="true" />
        <p>{text.loading}</p>
      </div> : !feed.assets.length ? <div className="pcb-state">
        <Images size={28} aria-hidden="true" />
        <p>{album?.kind === 'manual' ? collection.count(0) : Object.keys(location.filters).length || album?.kind === 'smart' ? filters.noMatch : text.noPhotos}</p>
        {Object.keys(location.filters).length
          ? <button type="button" className="pcb-control" onClick={() => navigate({ filters: {} })}>{filters.reset}</button>
          : location.month
          ? <button type="button" className="pcb-control" onClick={() => navigate({ month: '' })}>{text.chooseLatest}</button>
          : !album && <span>{text.quietEmpty}</span>}
      </div> : !!feed.next && <div className="pcb-page-status">
        <button type="button" className="pcb-control" onClick={feed.loadMore}>{text.loadMore}</button>
      </div>}
    </TimelineGrid>
    <Scrubber months={months} current={currentMonth || location.month} lang={location.lang}
      label={text.scrubber} jump={jump} />
    {location.photo && <Preview photo={location.photo} asset={feed.assets[index]} media={media}
      mediaLoading={!feed.completed}
      text={text} lang={location.lang} neighbors={neighbors} advance={advance} close={close}
      onLabels={(key, labels) => {
        setFavorite(key, labels.favorite)
        const values = album?.kind === 'smart' ? albumFilters(album) : location.filters
        if (values.favorite || values.tag?.startsWith('user:')) refreshLabels.current = true
      }}
      filterBy={(filter) => navigate({ album: '', filters: { ...location.filters, ...filter }, photo: '', month: '' })}
      openAlbum={openAlbum}
      previous={index > 0 ? () => openPhoto(-1) : undefined}
      next={index >= 0 && index < feed.assets.length - 1 ? () => openPhoto(1) : undefined} />}
    </div>
    {batchOpen && <SelectionActions assets={[...selected.values()]} current={album} text={collection} close={closeBatch}
      updated={(remaining) => {
        setSelected(new Map([...selected].filter(([, value]) => remaining.has(value.library))))
        setAnchor('')
        reload()
        if (album) refreshAlbums()
        if (!remaining.size) {
          setNotice(collection.finished)
          closeBatch()
        }
      }} />}
  </>
}

export default function Browser() {
  const [location, setLocation] = useState(readLocation)
  const [refresh, setRefresh] = useState(0)
  const [jumpRevision, setJumpRevision] = useState(0)
  const [filterOpen, setFilterOpen] = useState(false)
  const [albumEditor, setAlbumEditor] = useState<Album | 'new'>()
  const [deleting, setDeleting] = useState<Album>()
  const [albumVersion, setAlbumVersion] = useState(0)
  const refreshAlbums = () => setAlbumVersion((value) => value + 1)
  const [savingFilters, setSavingFilters] = useState(false)
  const [filterError, setFilterError] = useState(false)
  const filterButton = useRef<HTMLButtonElement>(null)
  const text = strings[location.lang]
  const filters = filterText[location.lang]
  const collection = collectionText[location.lang]
  const albumIndex = location.section === 'albums' && !location.album
  const albums = useRemote<{ items: Album[] }>(albumIndex ? albumUrl : null, albumVersion)
  const currentAlbum = useRemote<Album>(location.album ? albumUrl + '/' + location.album : null, albumVersion)
  const album = currentAlbum?.data
  const smart = album?.kind === 'smart'
  const scope = smart ? album.library ?? '' : location.library
  const activeFilters = Object.keys(smart ? albumFilters(album) : location.filters).length
  const libraries = useRemote<{ items: Library[] }>('/api/libraries', refresh)?.data?.items ?? []
  const result = useRemote<{ items: string[] }>(albumIndex ? null : browseUrl('months', scope, '', null, undefined, location.album), refresh + albumVersion)
  const months = result?.data?.items ?? []
  const options = location.month && !months.includes(location.month)
    ? [location.month, ...months] : months

  useEffect(() => {
    const update = () => setLocation(readLocation())
    window.addEventListener('popstate', update)
    return () => window.removeEventListener('popstate', update)
  }, [])
  useEffect(() => {
    document.documentElement.lang = location.lang
    document.title = (album?.name ?? (albumIndex ? collection.albums : text.timeline)) + ' · Photocull'
  }, [location.lang, album?.name, albumIndex, collection.albums, text.timeline])

  function navigate(updates: Partial<BrowserLocation>, replace = false) {
    const next = { ...location, ...updates }
    if (JSON.stringify(location) === JSON.stringify(next)) return
    writeLocation(next, replace)
    setLocation(next)
  }

  function goAlbum(item: Album) {
    navigate({ section: 'albums', album: item.id, library: '', month: '', photo: '', filters: {} })
  }

  function goTimeline() {
    navigate({ section: 'timeline', album: '', library: '', month: '', photo: '', filters: {} })
  }

  function closePhoto() {
    if (window.history.state?.photocullPhoto) window.history.back()
    else navigate({ photo: '' }, true)
  }

  function jump(month: string) {
    if (month === location.month) setJumpRevision((value) => value + 1)
    else navigate({ month })
  }

  function closeFilters() {
    setFilterOpen(false)
    setFilterError(false)
    requestAnimationFrame(() => filterButton.current?.focus())
  }

  function applyFilters(values: FilterValues) {
    if (smart) {
      setSavingFilters(true)
      setFilterError(false)
      void saveAlbum(album.name, 'smart', album.library ?? undefined, values, album).then(() => {
        setAlbumVersion((value) => value + 1)
        navigate({ month: '' }, true)
        closeFilters()
      }).catch(() => setFilterError(true)).finally(() => setSavingFilters(false))
    } else {
      navigate({ filters: values, month: '' })
      closeFilters()
    }
  }

  return <div className="pcb" lang={location.lang}>
    <header className="pcb-header">
      <div className="pcb-header-inner">
        <div className="pcb-brand"><Aperture size={23} aria-hidden="true" /><span>Photocull</span></div>
        <nav aria-label="Photocull">
          <span aria-current="page"><Images size={17} aria-hidden="true" />{text.browser}</span>
          <a href="/"><SlidersHorizontal size={17} aria-hidden="true" />{text.review}</a>
        </nav>
        <select className="pcb-language" aria-label={text.language} value={location.lang}
          onChange={(event) => navigate({ lang: event.target.value as Language })}>
          <option value="en" lang="en">EN</option>
          <option value="ja" lang="ja">日本語</option>
        </select>
      </div>
    </header>
    <div className="pcb-toolbar">
      <div className="pcb-title-block">
        {(albumIndex || location.album) && <button type="button" className="pcb-back-link"
          onClick={() => location.album ? navigate({ album: '', section: 'albums', photo: '', filters: {}, month: '', library: '' }) : goTimeline()}>
          ‹ {location.album ? collection.albums : collection.timeline}
        </button>}
        <h1>{album?.name ?? (location.section === 'albums' ? collection.albums : text.timeline)}</h1>
      </div>
      {albumIndex ? <button className="pcb-control pcb-album-link" type="button" onClick={() => setAlbumEditor('new')}>
        <FolderPlus size={17} aria-hidden="true" />{collection.new}
      </button> : <div className="pcb-tools">
        {!location.album && <button className="pcb-control pcb-album-link" type="button"
          title={collection.albums} aria-label={collection.albums} onClick={() => navigate({ section: 'albums', photo: '' })}>
          <Folder size={17} aria-hidden="true" /><span>{collection.albums}</span>
        </button>}
        {album && <button className="pcb-control pcb-album-link" type="button"
          title={collection.edit} aria-label={collection.edit} onClick={() => setAlbumEditor(album)}>
          <Pencil size={17} aria-hidden="true" />
        </button>}
        {!location.album && !!activeFilters && <button className="pcb-control pcb-album-link" type="button"
          aria-label={collection.new} title={collection.new} onClick={() => setAlbumEditor('new')}>
          <FolderPlus size={17} aria-hidden="true" />
        </button>}
        <button className="pcb-control pcb-filters-toggle" type="button" ref={filterButton}
          title={smart ? collection.editFilters : filters.title} aria-label={smart ? collection.editFilters : filters.title}
          aria-haspopup="dialog"
          data-active={activeFilters > 0} onClick={() => setFilterOpen(true)}>
          <ListFilter size={17} aria-hidden="true" />
          <span className="pcb-filter-label">{smart ? collection.editFilters : filters.title}</span>
          {activeFilters > 0 && <span>{activeFilters}</span>}
        </button>
        <label className="pcb-control pcb-library">
          <Images size={16} aria-hidden="true" />
          <select aria-label={text.library} value={scope} disabled={smart}
            onChange={(event) => navigate({ library: event.target.value, month: '' })}>
            <option value="">{text.all}</option>
            {location.library && !libraries.some(({ name }) => name === location.library) &&
              <option value={location.library}>{location.library}</option>}
            {libraries.map(({ name }) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        <label className="pcb-control pcb-month-select">
          <CalendarDays size={16} aria-hidden="true" />
          <select aria-label={text.month} value={location.month}
            onChange={(event) => jump(event.target.value)}>
            <option value="">{text.latest}</option>
            {options.map((month) => <option key={month} value={month}>
              {formatDate(month, location.lang, { year: 'numeric', month: 'short' })}
            </option>)}
          </select>
        </label>
        <label className="pcb-control pcb-density">
          <Grid2X2 size={16} aria-hidden="true" />
          <select aria-label={text.density} value={location.density}
            onChange={(event) => navigate({ density: event.target.value as Density })}>
            {densities.map((density) => <option key={density} value={density}>{text[density]}</option>)}
          </select>
        </label>
        <button className="pcb-control pcb-refresh" type="button" aria-label={text.refresh} title={text.refresh}
          onClick={() => setRefresh((value) => value + 1)}>
          <RefreshCw size={17} aria-hidden="true" />
        </button>
      </div>}
    </div>
    {albumIndex ? <Albums albums={albums?.data?.items} error={albums?.error} retryLabel={text.retry} text={collection}
      retry={() => setAlbumVersion((value) => value + 1)} open={goAlbum} edit={setAlbumEditor} onDelete={setDeleting} />
      : <Timeline key={JSON.stringify([location.album, location.library, location.month, location.filters,
        album?.revision, refresh, jumpRevision])}
        location={location} months={months} navigate={navigate} jump={jump} close={closePhoto} album={album}
        openAlbum={goAlbum} refreshAlbums={refreshAlbums} />}
    {filterOpen && <FiltersDialog location={smart ? { ...location, library: scope, filters: albumFilters(album) } : location}
      text={text} apply={applyFilters} close={closeFilters} busy={savingFilters}
      error={filterError ? collection.error : undefined} />}
    {albumEditor && <AlbumEditor album={albumEditor === 'new' ? undefined : albumEditor} location={location} libraries={libraries}
      allLibraries={text.all} libraryLabel={text.library} text={collection} close={() => setAlbumEditor(undefined)}
      saved={(item) => {
        setAlbumEditor(undefined)
        setAlbumVersion((value) => value + 1)
        if (albumEditor === 'new') goAlbum(item)
      }} />}
    {deleting && <DeleteAlbum album={deleting} text={collection} close={() => setDeleting(undefined)}
      deleted={() => { setDeleting(undefined); setAlbumVersion((value) => value + 1) }} />}
  </div>
}
