import { useState } from 'react'
import { Dialog } from './Dialog'
import type { FormEvent } from 'react'
import type { Asset } from './api'

const rejectReasons: Record<string, string> = {
  blur: 'Low sharpness',
  underexposed: 'Low exposure',
  overexposed: 'High exposure',
  clipped_highlights: 'Clipped highlights',
  clipped_shadows: 'Clipped shadows',
  burst_remaining: 'Burst reject',
}

export function ScoreBadges({ asset }: { asset: Asset }) {
  const scores = [
    ['Sharpness', asset.sharpness],
    ['Exposure', asset.exposure],
    ['Clipped highlights', asset.clipped_highlights],
    ['Clipped shadows', asset.clipped_shadows],
  ] as const
  return (
    <div className="score-badges">
      {scores.map(([label, value]) => value !== null && (
        <span key={label} className="score-badge">{label} <strong>{Math.round(value * 100)}%</strong></span>
      ))}
      {asset.auto_reject_reason && (
        <span className="score-badge flagged">{rejectReasons[asset.auto_reject_reason] ?? 'Auto-reject'}</span>
      )}
    </div>
  )
}

export function ConfirmAction({ title, description, action, onConfirm, onClose }: {
  title: string
  description: string
  action: string
  onConfirm: () => Promise<void>
  onClose: () => void
}) {
  const [busy, setBusy] = useState(false)

  async function confirm() {
    setBusy(true)
    await onConfirm()
    onClose()
  }

  return (
    <Dialog title={title} busy={busy} onClose={onClose}>
      <p>{description}</p>
      <div className="dialog-actions">
        <button autoFocus className="button secondary" disabled={busy} onClick={onClose}>Cancel</button>
        <button className="button primary" disabled={busy} onClick={confirm}>{action}</button>
      </div>
    </Dialog>
  )
}

export function AnalysisFilters({ active, onChange }: {
  active: boolean
  onChange: (filters: string) => void
}) {
  const [error, setError] = useState('')

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const params = new URLSearchParams()
    for (const key of ['min_sharpness', 'min_exposure', 'max_exposure']) {
      const value = String(form.get(key) ?? '')
      if (value !== '') params.set(key, String(Number(value) / 100))
    }
    const min = params.get('min_exposure')
    const max = params.get('max_exposure')
    if (min !== null && max !== null && Number(min) > Number(max)) {
      setError('Minimum exposure must be at most maximum exposure.')
      return
    }
    const flag = String(form.get('auto_reject') ?? '')
    if (flag) params.set('auto_reject', flag)
    setError('')
    onChange(params.toString())
  }

  return (
    <details className="analysis-filters">
      <summary>{active ? 'Score filters (active)' : 'Score filters'}</summary>
      <form onSubmit={apply} onReset={() => { setError(''); onChange('') }}>
        <label>Min sharpness (%) <input type="number" min="0" max="100" name="min_sharpness" /></label>
        <label>Min exposure (%) <input type="number" min="0" max="100" name="min_exposure" /></label>
        <label>Max exposure (%) <input type="number" min="0" max="100" name="max_exposure" /></label>
        <div className="filter-field">
          <label htmlFor="filter-auto-reject">Auto-reject</label>
          <select id="filter-auto-reject" name="auto_reject">
            <option value="">Any</option>
            <option value="true">Flagged</option>
            <option value="false">Unflagged</option>
          </select>
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="filter-actions">
          <button className="button secondary" type="submit">Apply</button>
          <button className="button quiet" type="reset">Reset</button>
        </div>
      </form>
    </details>
  )
}
