import { readFilters, writeFilters } from './filterValues'
import type { FilterValues } from './filterValues'

export type Density = 'compact' | 'comfortable' | 'large'
export type Language = 'en' | 'ja'

export type BrowserLocation = {
  section: 'timeline' | 'albums'
  album: string
  library: string
  month: string
  density: Density
  lang: Language
  photo: string
  filters: FilterValues
}

export const densities: Density[] = ['compact', 'comfortable', 'large']

export function readLocation(): BrowserLocation {
  const query = new URLSearchParams(window.location.search)
  const album = query.get('album') ?? ''
  const library = query.get('library') ?? ''
  const month = query.get('month') ?? ''
  const density = query.get('density')
  const lang = query.get('lang')
  const photo = query.get('photo') ?? ''
  return {
    section: query.get('section') === 'albums' ? 'albums' : 'timeline',
    album: /^[0-9a-f]{32}$/.test(album) ? album : '',
    library: /^[\w-]{1,64}$/.test(library) ? library : '',
    month: /^\d{4}-(0[1-9]|1[0-2])$/.test(month) ? month : '',
    density: density === 'compact' || density === 'large' ? density : 'comfortable',
    lang: lang === 'ja' || lang === 'en' ? lang : navigator.language.startsWith('ja') ? 'ja' : 'en',
    photo: /^[\w-]{1,64}:[1-9]\d{0,18}$/.test(photo) ? photo : '',
    filters: readFilters(query),
  }
}

export function writeLocation(next: BrowserLocation, replace = false) {
  const query = new URLSearchParams({ view: 'browser', lang: next.lang })
  if (next.section === 'albums') query.set('section', next.section)
  if (next.album) query.set('album', next.album)
  if (next.library) query.set('library', next.library)
  if (next.month) query.set('month', next.month)
  if (next.density !== 'comfortable') query.set('density', next.density)
  if (next.photo) query.set('photo', next.photo)
  writeFilters(query, next.filters)
  const method = replace ? 'replaceState' : 'pushState'
  window.history[method]({
    photocullPhoto: replace ? window.history.state?.photocullPhoto : !!next.photo,
  }, '', '?' + query)
}
