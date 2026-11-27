import { useRef, useState } from 'react'
import type { Language } from './location'
import { formatDate } from './strings'

export function Scrubber({ months, current, lang, label, jump }: {
  months: string[]
  current: string
  lang: Language
  label: string
  jump: (month: string) => void
}) {
  const dragging = useRef(false)
  const [draft, setDraft] = useState<number>()
  const currentIndex = Math.max(0, months.indexOf(current))
  const index = draft ?? currentIndex
  const active = months[index]

  if (months.length < 2) return null

  const years: { text: string; index: number }[] = []
  months.forEach((month, i) => {
    const year = month.slice(0, 4)
    const last = years.at(-1)
    if (last?.text !== year && (!last || i - last.index >= Math.max(1, months.length / 11))) {
      years.push({ text: year, index: i })
    }
  })

  return <aside className="pcb-scrubber">
    <div className="pcb-scrubber-inner">
      <div className="pcb-scrubber-labels" aria-hidden="true">
        {years.map((year) => <span key={year.text}
          style={{ top: 100 * year.index / (months.length - 1) + '%' }}>{year.text}</span>)}
      </div>
      <input className="pcb-range" type="range" min={0} max={months.length - 1}
        value={index} aria-label={label} aria-orientation="vertical"
        aria-valuetext={formatDate(active, lang, { year: 'numeric', month: 'long' })}
        onPointerDown={() => { dragging.current = true; setDraft(currentIndex) }}
        onPointerUp={(event) => {
          if (!dragging.current) return
          dragging.current = false
          const value = Number(event.currentTarget.value)
          setDraft(undefined)
          if (value !== currentIndex) jump(months[value])
        }}
        onPointerCancel={() => { dragging.current = false; setDraft(undefined) }}
        onChange={(event) => {
          const value = Number(event.currentTarget.value)
          if (dragging.current) setDraft(value)
          else jump(months[value])
        }} />
      <output className="pcb-scrubber-date" aria-hidden="true" hidden={draft === undefined}
        style={{ top: 100 * index / (months.length - 1) + '%' }}>
        {formatDate(active, lang, { year: 'numeric', month: 'long' })}
      </output>
    </div>
  </aside>
}
