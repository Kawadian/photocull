import { useState } from 'react'
import { json, message, request, useRemote } from './api'
import { Dialog } from './Dialog'

type Selection = { tags: 'local' | 'openai'; flags: 'local' | 'openai' }
type ProviderStatus = { selection: Selection; pending: number }

export function Providers({ base, library, version, onClose }: {
  base: string; library: string; version?: number; onClose: () => void
}) {
  const url = `${base}/providers`
  const [reload, setReload] = useState(0)
  const status = useRemote<ProviderStatus>(url, (version ?? 0) + reload)
  const [draft, setDraft] = useState<Selection>()
  const [saved, setSaved] = useState<Selection>()
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const selection = draft ?? saved ?? status?.data?.selection

  async function run(action: () => Promise<string>) {
    setBusy(true)
    setNotice('')
    setError('')
    try {
      setNotice(await action())
      setReload((value) => value + 1)
    } catch (failure) {
      setError(message(failure))
    } finally {
      setBusy(false)
    }
  }

  return <Dialog title={`Decision providers · ${library}`} busy={busy} onClose={onClose}>
    {!selection && !status?.error && <p role="status">Loading provider settings…</p>}
    {status?.error && <p role="alert">{status.error}</p>}
    {selection && <div className="provider-settings">
      <form onSubmit={(event) => {
        event.preventDefault()
        void run(async () => {
          const result = await request<{ selection: Selection }>(url, json('PUT', selection))
          setSaved(result.selection)
          setDraft(undefined)
          return 'Provider choices saved for this library.'
        })
      }}>
        <fieldset disabled={busy}>
          <legend>Provider choices</legend>
          {(['tags', 'flags'] as const).map((kind) => <label key={kind}>
            {kind === 'tags' ? 'Automatic tags' : 'Scores and auto-reject'}
            <select value={selection[kind]} onChange={(event) => setDraft({
              ...selection, [kind]: event.target.value as Selection[typeof kind],
            })}>
              <option value="local">Local</option>
              <option value="openai">OpenAI compatible</option>
            </select>
          </label>)}
          <p>OpenAI-compatible analysis sends reduced photos to the server configured by your administrator.</p>
          <div className="provider-actions">
            <button className="button primary" disabled={!draft} type="submit">Save provider choices</button>
            <button className="button secondary" type="button" onClick={() => void run(async () => {
              const response = await fetch(url, { method: 'DELETE' })
              if (!response.ok) throw new Error('Could not restore YAML defaults.')
              setSaved((await request<ProviderStatus>(url)).selection)
              setDraft(undefined)
              return 'YAML defaults restored.'
            })}>Restore YAML defaults</button>
            <button className="button secondary" type="button" onClick={() => void run(async () => {
              const result = await request<{ ok: boolean; endpoint: string }>(
                `${url}/test`, { method: 'POST' },
              )
              return `${result.ok ? 'Connection succeeded' : 'Connection failed'} · ${result.endpoint}`
            })}>Test connection</button>
          </div>
        </fieldset>
      </form>
      <form onSubmit={(event) => {
        event.preventDefault()
        const data = new FormData(event.currentTarget)
        void run(async () => {
          const { queued } = await request<{ queued: number }>(`${url}/reanalyze`, json('POST', {
            kind: data.get('kind'), scope: data.get('scope'),
            start: data.get('start') || null, end: data.get('end') || null,
          }))
          return `${queued} automatic decisions queued.`
        })
      }}>
        <fieldset disabled={busy || !!draft}>
          <legend>Reanalyze existing photos</legend>
          <label>Decisions
            <select name="kind">
              <option value="flags">Scores and auto-reject</option>
              <option value="tags">Automatic tags</option>
              <option value="both">Tags and auto-reject</option>
            </select>
          </label>
          <label>Include photos with
            <select name="scope">
              <option value="unjudged">No decision</option>
              <option value="all">Any decision status</option>
              <option value="local">Local results</option>
              <option value="openai">OpenAI results</option>
            </select>
          </label>
          <label>From <input name="start" type="date" /></label>
          <label>Through <input name="end" type="date" /></label>
          <p>Reanalysis preserves manual tags, ratings, rejections and cleared flags.</p>
          <button className="button secondary" type="submit">Queue reanalysis</button>
        </fieldset>
      </form>
      {!!status?.data?.pending && <p>{status.data.pending} automatic decisions pending.</p>}
    </div>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    <div className="dialog-actions">
      <button className="button secondary" onClick={onClose} disabled={busy}>Close</button>
    </div>
  </Dialog>
}
