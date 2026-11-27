import { ScanFace, Star } from 'lucide-react'
import type { ReactNode } from 'react'
import type { Language } from './location'
import { formatDate } from './strings'
import type { Strings } from './strings'
import type { ViewerAsset } from './viewer'

function Fact({ label, children }: { label: string; children: ReactNode }) {
  if (children == null || children === '') return null
  return <div><dt>{label}</dt><dd>{children}</dd></div>
}

function exposure(value: number | null, number: Intl.NumberFormat) {
  if (!value) return null
  if (value >= .3) return number.format(value) + ' s'
  return '1/' + Math.round(1 / value) + ' s'
}

export function PhotoInformation({ asset, loading, error, retry, text, lang, boxes, toggleBoxes,
  rate, busy }: {
  asset?: ViewerAsset
  loading: boolean
  error?: string
  retry: () => void
  text: Strings
  lang: Language
  boxes: boolean
  toggleBoxes: () => void
  rate: (value: number) => void
  busy: boolean
}) {
  const number = new Intl.NumberFormat(lang, { maximumFractionDigits: 1 })
  const percent = new Intl.NumberFormat(lang, { style: 'percent', maximumFractionDigits: 1 })
  const date = asset?.captured_at
    ? new Date(asset.captured_at).toLocaleString(lang, { dateStyle: 'medium', timeStyle: 'short' })
    : asset?.day ? formatDate(asset.day, lang, { dateStyle: 'medium' }) : null
  const size = asset && (asset.size < 1024 ? asset.size + ' B'
    : asset.size < 1024 ** 2 ? number.format(asset.size / 1024) + ' KiB'
    : number.format(asset.size / 1024 ** 2) + ' MiB')
  const people = [...new Map(asset?.faces.map((face) => [face.person_id, face.name]) ?? []).entries()]

  return <aside className="pcb-information" aria-label={text.information}>
    <h2>{text.information}</h2>
    {!asset ? <div className="pcb-information-status" role="status">
      <p>{error ? text.photoError : text.loadingPhoto}</p>
      {error && <button type="button" className="pcb-control" onClick={retry}>{text.retry}</button>}
    </div> : <>
      <div className="pcb-stars" role="group" aria-label={text.rating}>
        {[1, 2, 3, 4, 5].map((rating) => <button type="button" className="pcb-icon" key={rating}
          aria-label={text.rateStars(rating)} aria-pressed={asset.rating === rating}
          aria-keyshortcuts={String(rating)} disabled={busy}
          onClick={() => rate(asset.rating === rating ? 0 : rating)}>
          <Star size={18} fill={asset.rating >= rating ? 'currentColor' : 'none'} aria-hidden="true" />
        </button>)}
      </div>
      <dl className="pcb-facts">
        <Fact label={text.captured}>{date}</Fact>
        <Fact label={text.camera}>{[asset.make, asset.model].filter(Boolean).join(' ')}</Fact>
        <Fact label={text.lens}>{asset.lens}</Fact>
        <Fact label={text.exposureTime}>{exposure(asset.shutter_seconds, number)}</Fact>
        <Fact label={text.aperture}>{asset.aperture ? 'ƒ/' + number.format(asset.aperture) : null}</Fact>
        <Fact label={text.focalLength}>{asset.focal_length_mm ? number.format(asset.focal_length_mm) + ' mm' : null}</Fact>
        <Fact label="ISO">{asset.iso ? number.format(asset.iso) : null}</Fact>
        <Fact label="GPS">{asset.latitude != null && asset.longitude != null
          ? asset.latitude.toFixed(5) + ', ' + asset.longitude.toFixed(5) : null}</Fact>
        <Fact label={text.sharpness}>{asset.sharpness != null ? percent.format(asset.sharpness) : null}</Fact>
        <Fact label={text.aesthetic}>{asset.insights?.aesthetic?.score != null
          ? percent.format(asset.insights.aesthetic.score) : null}</Fact>
      </dl>
      {loading && <p className="pcb-information-status" role="status">{text.preparingMetadata}</p>}
      {!!asset.tags?.length && <section>
        <h3>{text.tags}</h3>
        <ul className="pcb-information-chips">{asset.tags.map((tag) =>
          <li key={tag.tag}>{tag.label}</li>)}</ul>
      </section>}
      {!!people.length && <section>
        <h3>{text.people}</h3>
        <ul className="pcb-information-chips">{people.map(([id, name]) =>
          <li key={id}>{name || text.unnamedPerson}</li>)}</ul>
        <button type="button" className="pcb-control" aria-pressed={boxes} onClick={toggleBoxes}>
          <ScanFace size={16} aria-hidden="true" /> {text.faceBoxes}
        </button>
      </section>}
      <dl className="pcb-facts">
        <Fact label={text.dimensions}>{asset.width && asset.height ? asset.width + ' × ' + asset.height : null}</Fact>
        <Fact label={text.fileSize}>{size}</Fact>
        <Fact label={text.filePath}><span className="pcb-file-path">{asset.path}</span></Fact>
      </dl>
    </>}
  </aside>
}
