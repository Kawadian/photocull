import type { Density } from './location'
import type { TimelineAsset } from './timeline'
import { assetKey } from './timeline'

type Heading = { key: string; top: number; height: number; day: string }
  & ({ type: 'year' } | { type: 'month' } | { type: 'day' })
type Cell = { width: number; index: number }
export type PhotoRow = { key: string; top: number; height: number; type: 'photos'; cells: Cell[] }
export type Row = Heading | PhotoRow
export const gap = 6

function aspect(asset: TimelineAsset) {
  if (!asset.width || !asset.height) return 4 / 3
  return Math.max(0.45, Math.min(3.5, asset.width / asset.height))
}

export function layout(assets: TimelineAsset[], width: number, density: Density) {
  const target = width < 560
    ? { compact: 84, comfortable: 123, large: 197 }[density]
    : { compact: 133, comfortable: 203, large: 306 }[density]
  const rows: Row[] = []
  const assetRows: number[] = []
  let top = 0
  let lastDay = ''
  let index = 0

  function heading(type: Heading['type'], day: string, height: number) {
    rows.push({ type, day, height, key: type + day, top })
    top += height
  }

  while (width > 0 && index < assets.length) {
    const first = index
    const day = assets[first].day
    if (day !== lastDay) {
      if (day.slice(0, 4) !== lastDay.slice(0, 4)) heading('year', day, 48)
      if (day.slice(0, 7) !== lastDay.slice(0, 7)) heading('month', day, 40)
      heading('day', day, 37)
      lastDay = day
    }
    let ratio = 0
    while (index < assets.length && assets[index].day === day) {
      const next = aspect(assets[index])
      const gaps = (index - first) * gap
      const fit = (width - gaps) / (ratio + next)
      if (index > first && fit < target) {
        const previousFit = (width - (gaps - gap)) / ratio
        if (previousFit - target < target - fit) break
      }
      ratio += next
      index++
      if (fit <= target) break
    }
    const count = index - first
    const room = width - (count - 1) * gap
    const fitted = room / ratio
    const dayEnds = index === assets.length || assets[index].day !== day
    const height = Math.max(1, Math.min(fitted, dayEnds ? target : target * 1.28))
    let right = 0
    const cells = assets.slice(first, index).map((asset, offset) => {
      const next = right + aspect(asset) * height
      const width = Math.max(1, Math.round(next) - Math.round(right))
      right = next
      assetRows.push(rows.length)
      return { width, index: first + offset }
    })
    rows.push({ type: 'photos', key: assetKey(assets[first]), top, height, cells })
    top += height + gap
  }
  return { rows, height: top, assetRows }
}

export function rowAt(rows: Row[], y: number) {
  let left = 0
  let right = rows.length
  while (left < right) {
    const mid = (left + right) >>> 1
    if (rows[mid].top + rows[mid].height < y) left = mid + 1
    else right = mid
  }
  return left
}

export function neighbor(rows: Row[], rowIndex: number, index: number, key: string, count: number) {
  const row = rows[rowIndex] as PhotoRow
  if (key === 'ArrowLeft') return Math.max(0, index - 1)
  if (key === 'ArrowRight') return Math.min(count - 1, index + 1)
  if (key === 'Home') return row.cells[0].index
  if (key === 'End') return row.cells.at(-1)!.index
  if (key !== 'ArrowUp' && key !== 'ArrowDown') return null

  const direction = key === 'ArrowUp' ? -1 : 1
  let x = 0
  for (const cell of row.cells) {
    if (cell.index === index) {
      x += cell.width / 2
      break
    }
    x += cell.width + gap
  }
  for (let i = rowIndex + direction; i >= 0 && i < rows.length; i += direction) {
    const candidate = rows[i]
    if (candidate.type !== 'photos') continue
    let edge = 0
    for (const cell of candidate.cells) {
      edge += cell.width + gap
      if (edge > x) return cell.index
    }
    return candidate.cells.at(-1)!.index
  }
  return index
}
