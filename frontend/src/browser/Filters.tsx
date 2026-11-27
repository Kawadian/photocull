import { X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useRemote } from '../api'
import type { FilterName, FilterValues } from './filterValues'
import type { BrowserLocation, Language } from './location'
import type { Strings } from './strings'
import { browseUrl } from './timeline'
import './filters.css'

type Option = { value: string; label: string; count?: number; source?: 'auto' | 'user' }
type Facets = Record<'tags' | 'people' | 'cameras' | 'lenses' | 'places', Option[]>

const en = {
  title: 'Filters',
  any: 'Any',
  kind: 'Media',
  photos: 'Photos',
  favorites: 'Favorites',
  flagged: 'Flag',
  manual: 'Manually rejected',
  automatic: 'Automatically flagged',
  clear: 'Unflagged',
  tag: 'Tag',
  user: 'User tags',
  auto: 'Automatic tags',
  place: 'Place',
  person: 'Person',
  start: 'From date',
  end: 'To date',
  ratingMin: 'Minimum rating',
  ratingMax: 'Maximum rating',
  sharpMin: 'Minimum sharpness',
  sharpMax: 'Maximum sharpness',
  aestheticMin: 'Minimum aesthetic score',
  aestheticMax: 'Maximum aesthetic score',
  loading: 'Loading filter options…',
  unavailable: 'Could not load filter options.',
  apply: 'Apply filters',
  reset: 'Clear filters',
  close: 'Close filters',
  noMatch: 'No photos or videos match these filters',
}

export const filterText: Record<Language, typeof en> = {
  en,
  ja: {
    title: '絞り込み',
    any: 'すべて',
    kind: 'メディア',
    photos: '写真',
    favorites: 'お気に入り',
    flagged: 'フラグ',
    manual: '手動で除外',
    automatic: '自動でフラグ',
    clear: 'フラグなし',
    tag: 'タグ',
    user: 'ユーザータグ',
    auto: '自動タグ',
    place: '場所',
    person: '人物',
    start: '開始日',
    end: '終了日',
    ratingMin: '評価の下限',
    ratingMax: '評価の上限',
    sharpMin: 'シャープネスの下限',
    sharpMax: 'シャープネスの上限',
    aestheticMin: '美的スコアの下限',
    aestheticMax: '美的スコアの上限',
    loading: '候補を読み込み中…',
    unavailable: '絞り込み候補を読み込めませんでした。',
    apply: '適用',
    reset: '絞り込みを解除',
    close: '絞り込みを閉じる',
    noMatch: '条件に一致する写真や動画がありません',
  },
}

export function FiltersDialog({ location, apply, close, text }: {
  location: BrowserLocation
  apply: (values: FilterValues) => void
  close: () => void
  text: Strings
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [draft, setDraft] = useState(location.filters)
  const [retry, setRetry] = useState(0)
  const result = useRemote<Facets>(browseUrl('facets', location.library), retry)
  const facets = result?.data
  const labels = filterText[location.lang]

  useEffect(() => {
    const modal = dialog.current!
    modal.showModal()
    return () => modal.close()
  }, [])

  function set(name: FilterName, value: string) {
    setDraft((previous) => {
      const next = { ...previous }
      if (value) next[name] = value
      else delete next[name]
      return next
    })
  }

  function options(items: Option[]) {
    return items.map(({ label, value, count }) => <option key={value} value={value}>
      {label}{count === undefined ? '' : ' (' + count.toLocaleString(location.lang) + ')'}
    </option>)
  }

  function select(name: FilterName, label: string, items: Option[]) {
    const value = draft[name] ?? ''
    const unmatched = value && !items.some((item) => item.value === value)
    return <label className="pcb-filter-field">{label}
      <select value={value} onChange={(event) => set(name, event.target.value)}>
        <option value="">{labels.any}</option>
        {unmatched && <option value={value}>{value.replace(/^(auto|user|name):/, '')}</option>}
        {name === 'tag' ? <>
          <optgroup label={labels.user}>{options(items.filter((item) => item.source === 'user'))}</optgroup>
          <optgroup label={labels.auto}>{options(items.filter((item) => item.source === 'auto'))}</optgroup>
        </> : options(items)}
      </select>
    </label>
  }

  function score(name: FilterName, label: string, min = '0', max = '1') {
    return <label className="pcb-filter-field">{label}
      <input type="number" min={min} max={max} step=".01" placeholder="0–1" value={draft[name] ?? ''}
        onChange={(event) => set(name, event.target.value)} />
    </label>
  }

  function dates(name: 'start' | 'end', label: string) {
    return <label className="pcb-filter-field">{label}
      <input type="date" value={draft[name] ?? ''} min={name === 'end' ? draft.start : undefined}
        max={name === 'start' ? draft.end : undefined} onChange={(event) => set(name, event.target.value)} />
    </label>
  }

  const ratings = [0, 1, 2, 3, 4, 5].map((rating) => ({ label: String(rating), value: String(rating) }))

  return <dialog className="pcb-filter-dialog" ref={dialog}
    aria-label={labels.title} onCancel={(event) => { event.preventDefault(); close() }}
    onClick={(event) => { if (event.target === event.currentTarget) close() }}>
    <form className="pcb-filter-form" onSubmit={(event) => { event.preventDefault(); apply(draft) }}>
      <header>
        <h2>{labels.title}</h2>
        <button type="button" className="pcb-control" title={labels.close} aria-label={labels.close} onClick={close}>
          <X size={18} aria-hidden="true" />
        </button>
      </header>
      <div className="pcb-filter-fields">
        {select('kind', labels.kind, [
          { label: labels.photos, value: 'photo' },
          { label: text.video, value: 'video' },
          { label: 'RAW', value: 'raw' },
          { label: 'HEIF', value: 'heif' },
        ])}
        {select('flag', labels.flagged, [
          { label: labels.manual, value: 'manual' },
          { label: labels.automatic, value: 'automatic' },
          { label: labels.clear, value: 'clear' },
        ])}
        {select('tag', labels.tag, facets?.tags ?? [])}
        {select('person', labels.person, facets?.people ?? [])}
        {select('camera', text.camera, facets?.cameras ?? [])}
        {select('lens', text.lens, facets?.lenses ?? [])}
        <div className="pcb-filter-flags">
          {(!!facets?.places.length || !!draft.place) && select('place', labels.place, facets?.places ?? [])}
          <label className="pcb-favorite-filter">
            <input type="checkbox" checked={draft.favorite === 'true'}
              onChange={(event) => set('favorite', event.target.checked ? 'true' : '')} /> {labels.favorites}
          </label>
        </div>
        {dates('start', labels.start)}
        {dates('end', labels.end)}
        {select('min_rating', labels.ratingMin, ratings.filter((entry) =>
          draft.max_rating == null || Number(entry.value) <= Number(draft.max_rating)))}
        {select('max_rating', labels.ratingMax, ratings.filter((entry) =>
          draft.min_rating == null || Number(entry.value) >= Number(draft.min_rating)))}
        {score('min_sharpness', labels.sharpMin, '0', draft.max_sharpness ?? '1')}
        {score('max_sharpness', labels.sharpMax, draft.min_sharpness ?? '0')}
        {score('min_aesthetic', labels.aestheticMin, '0', draft.max_aesthetic ?? '1')}
        {score('max_aesthetic', labels.aestheticMax, draft.min_aesthetic ?? '0')}
        {(!result || result.error) && <div className="pcb-filter-status" role="status">
          {result?.error ? labels.unavailable : labels.loading}
          {result?.error && <button type="button" className="pcb-control" onClick={() => setRetry((value) => value + 1)}>
            {text.retry}
          </button>}
        </div>}
      </div>
      <footer>
        <button type="button" className="pcb-control" onClick={() => apply({})}>{labels.reset}</button>
        <button type="submit" className="pcb-control pcb-apply">{labels.apply}</button>
      </footer>
    </form>
  </dialog>
}
