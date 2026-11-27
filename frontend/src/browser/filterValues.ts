export const filterFields = [
  'kind', 'favorite', 'flag', 'tag', 'person', 'camera', 'lens', 'place', 'start', 'end',
  'min_rating', 'max_rating', 'min_sharpness', 'max_sharpness', 'min_aesthetic', 'max_aesthetic',
] as const

export type FilterName = typeof filterFields[number]
export type FilterValues = Partial<Record<FilterName, string>>

export function readFilters(query: URLSearchParams): FilterValues {
  const filters: FilterValues = {}
  for (const field of filterFields) {
    const value = query.get(field)?.trim()
    if (value && value.length <= 160) filters[field] = value
  }
  return filters
}

export function writeFilters(query: URLSearchParams, filters: FilterValues) {
  for (const field of filterFields) {
    if (filters[field]) query.set(field, filters[field])
  }
}
