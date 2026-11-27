import { Aperture, CalendarDays, Grid2X2, Images, ListFilter, RefreshCw, SlidersHorizontal } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useRemote } from '../api'
import type { Library } from '../api'
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

function Timeline({ location, months, navigate, jump, close }: {
  location: BrowserLocation
  months: string[]
  navigate: Navigate
  jump: (month: string) => void
  close: () => void
}) {
  const text = strings[location.lang]
  const filters = filterText[location.lang]
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

  return <div className="pcb-content">
    <TimelineGrid assets={feed.assets} location={location} recipe={media?.recipe} text={text} busy={feed.busy}
      canLoad={!!feed.next && !feed.error} loadMore={feed.loadMore} onMonth={setCurrentMonth}
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
        <p>{Object.keys(location.filters).length ? filters.noMatch : text.noPhotos}</p>
        {Object.keys(location.filters).length
          ? <button type="button" className="pcb-control" onClick={() => navigate({ filters: {} })}>{filters.reset}</button>
          : location.month
          ? <button type="button" className="pcb-control" onClick={() => navigate({ month: '' })}>{text.chooseLatest}</button>
          : <span>{text.quietEmpty}</span>}
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
        if (location.filters.favorite || location.filters.tag?.startsWith('user:')) refreshLabels.current = true
      }}
      filterBy={(filter) => navigate({ filters: { ...location.filters, ...filter }, photo: '', month: '' })}
      previous={index > 0 ? () => openPhoto(-1) : undefined}
      next={index >= 0 && index < feed.assets.length - 1 ? () => openPhoto(1) : undefined} />}
  </div>
}

export default function Browser() {
  const [location, setLocation] = useState(readLocation)
  const [refresh, setRefresh] = useState(0)
  const [jumpRevision, setJumpRevision] = useState(0)
  const [filterOpen, setFilterOpen] = useState(false)
  const filterButton = useRef<HTMLButtonElement>(null)
  const text = strings[location.lang]
  const filters = filterText[location.lang]
  const activeFilters = Object.keys(location.filters).length
  const libraries = useRemote<{ items: Library[] }>('/api/libraries', refresh)?.data?.items ?? []
  const result = useRemote<{ items: string[] }>(browseUrl('months', location.library), refresh)
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
    document.title = text.timeline + ' · Photocull'
  }, [location.lang, text.timeline])

  function navigate(updates: Partial<BrowserLocation>, replace = false) {
    const next = { ...location, ...updates }
    if (JSON.stringify(location) === JSON.stringify(next)) return
    writeLocation(next, replace)
    setLocation(next)
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
    requestAnimationFrame(() => filterButton.current?.focus())
  }

  function applyFilters(values: FilterValues) {
    navigate({ filters: values, month: '' })
    closeFilters()
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
      <h1>{text.timeline}</h1>
      <div className="pcb-tools">
        <button className="pcb-control pcb-filters-toggle" type="button" ref={filterButton}
          title={filters.title} aria-label={filters.title} aria-haspopup="dialog"
          data-active={activeFilters > 0} onClick={() => setFilterOpen(true)}>
          <ListFilter size={17} aria-hidden="true" />
          <span className="pcb-filter-label">{filters.title}</span>
          {activeFilters > 0 && <span>{activeFilters}</span>}
        </button>
        <label className="pcb-control pcb-library">
          <Images size={16} aria-hidden="true" />
          <select aria-label={text.library} value={location.library}
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
      </div>
    </div>
    <Timeline key={JSON.stringify([location.library, location.month, location.filters, refresh, jumpRevision])}
      location={location} months={months} navigate={navigate} jump={jump} close={closePhoto} />
    {filterOpen && <FiltersDialog location={location} text={text} apply={applyFilters} close={closeFilters} />}
  </div>
}
