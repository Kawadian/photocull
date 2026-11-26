import { Check, FileOutput, Star, Undo2, X } from 'lucide-react'
import { useState } from 'react'
import { Dialog } from './Dialog'
import { assetName, json, message, request } from './api'
import { ScoreBadges } from './Analysis'
import type { Asset, XmpPlan } from './api'

const scoreStates = {
  pending: 'Scoring queued',
  running: 'Scoring in progress',
  error: 'Scoring failed',
  skipped: 'Scoring skipped',
  done: 'Scores unavailable',
}

type InspectorProps = {
  base: string
  asset: Asset | undefined
  busy: boolean
  undoAvailable: boolean
  onRating: (asset: Asset, rating: number, rejected?: boolean) => void
  onUndo: () => void
  onClearFlag: (asset: Asset) => void
}

function XmpDialog({ plan, onClose, onWrite }: {
  plan: XmpPlan
  onClose: () => void
  onWrite: () => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  async function apply() {
    setBusy(true)
    setError(undefined)
    try {
      await onWrite()
      onClose()
    } catch (failure) {
      setError(message(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog title={plan.action === 'unchanged' ? 'Sidecar is up to date' : 'Write XMP sidecar?'}
      onClose={onClose} busy={busy}>
      <p>{plan.action === 'create' ? 'Create' : plan.action === 'update' ? 'Update' : 'Current sidecar'}</p>
      <code>{plan.sidecar}</code>
      <p>XMP rating <strong>{plan.rating}</strong></p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="dialog-actions">
        <button autoFocus className="button secondary" onClick={onClose} disabled={busy}>
          {plan.action === 'unchanged' ? 'Close' : 'Cancel'}
        </button>
        {plan.action !== 'unchanged' && (
          <button className="button primary" disabled={busy} onClick={apply}>
            {busy ? 'Writing…' : 'Write sidecar'}
          </button>
        )}
      </div>
    </Dialog>
  )
}

function ExportButton({ base, asset }: { base: string; asset: Asset }) {
  const [plan, setPlan] = useState<XmpPlan>()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string>()
  const [error, setError] = useState<string>()
  const url = `${base}/assets/${asset.id}/xmp`

  async function preview() {
    setBusy(true)
    setNote(undefined)
    setError(undefined)
    try {
      setPlan(await request<XmpPlan>(url, json('POST', { dry_run: true })))
    } catch (failure) {
      setError(message(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button className="button secondary xmp-button" onClick={preview} disabled={busy}>
        <FileOutput size={16} />
        {busy ? 'Checking sidecar…' : 'Write XMP…'}
      </button>
      {note && <p className="export-success" role="status"><Check size={14} /> {note}</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      {plan && <XmpDialog
        plan={plan}
        onClose={() => setPlan(undefined)}
        onWrite={async () => {
          const result = await request<XmpPlan>(url, json('POST', {
            dry_run: false,
            confirmation: plan.confirmation,
          }))
          setNote(result.action === 'unchanged' ? 'Sidecar is up to date' : 'Sidecar written')
        }}
      />}
    </>
  )
}

export function Inspector({ base, asset, busy, undoAvailable, onRating, onUndo, onClearFlag }: InspectorProps) {
  const camera = [asset?.make, asset?.model].filter(Boolean).join(' ')
  const duration = asset?.duration
  const filename = asset ? assetName(asset) : ''
  const dot = filename.lastIndexOf('.')

  return (
    <aside className="inspector" aria-label="Asset information and ratings">
      <div className="inspector-section">
        <div className="section-heading">Rating</div>
        <div className="stars" aria-label="Rate asset">
          {[1, 2, 3, 4, 5].map((value) => (
            <button
              key={value}
              className={asset && asset.rating >= value ? 'filled' : undefined}
              aria-label={`Rate ${value} ${value === 1 ? 'star' : 'stars'}`}
              aria-pressed={asset?.rating === value}
              title={`Rate ${value}`}
              disabled={busy || !asset}
              onClick={() => asset && onRating(asset, value)}
            ><Star size={21} /></button>
          ))}
        </div>
        <div className="rating-actions">
          <button
            className="button secondary reject"
            aria-pressed={!!asset?.rejected}
            disabled={busy || !asset}
            onClick={() => asset && onRating(asset, 0, true)}
          ><X size={15} /> Reject <kbd>X</kbd></button>
          <button
            className="button secondary"
            disabled={busy || !asset || (!asset.rating && !asset.rejected)}
            onClick={() => asset && onRating(asset, 0)}
          >Unrate <kbd>0</kbd></button>
        </div>
        <button className="button quiet undo" disabled={busy || !undoAvailable} onClick={onUndo}>
          <Undo2 size={16} /> Undo rating <kbd>U</kbd>
        </button>
      </div>
      {asset && (
        <>
          <div className="inspector-section">
            <div className="section-heading">Scores</div>
            {(asset.sharpness !== null || asset.auto_reject) && <ScoreBadges asset={asset} />}
            {asset.kind === 'video' ? <p className="score-state">Scores apply to still images.</p>
              : (asset.sharpness === null || (asset.score_status && asset.score_status !== 'done')) && (
                <p className={asset.score_status === 'error' ? 'inline-error' : 'score-state'} role="status">
                  {asset.score_status ? scoreStates[asset.score_status] : 'Awaiting scores'}
                  {asset.score_error && <> · {asset.score_error}</>}
                </p>
              )}
            {asset.auto_reject && (
              <button className="button quiet clear-flag" disabled={busy} onClick={() => onClearFlag(asset)}>
                Clear auto-reject
              </button>
            )}
          </div>
          <div className="inspector-section file-info">
            <div className="section-heading">File details</div>
            <div className="file-name" title={asset.path}>{filename}</div>
            <dl>
              {dot !== -1 && <><dt>Format</dt><dd>{filename.slice(dot + 1).toUpperCase()}</dd></>}
              <dt>{asset.captured_at ? 'Captured' : 'File day'}</dt>
              <dd>{asset.captured_at?.replace('T', ' ') ?? asset.day}</dd>
              {camera && <><dt>Camera</dt><dd>{camera}</dd></>}
              {asset.width !== null && asset.height !== null && (
                <><dt>Image</dt><dd>{asset.width} × {asset.height}</dd></>
              )}
              {duration != null && <><dt>Duration</dt><dd>{duration.toFixed(1)} s</dd></>}
            </dl>
          </div>
          <div className="inspector-section">
            <div className="section-heading">Lightroom sidecar</div>
            <ExportButton key={asset.id} base={base} asset={asset} />
          </div>
        </>
      )}
    </aside>
  )
}
