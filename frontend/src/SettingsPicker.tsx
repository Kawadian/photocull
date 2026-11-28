import { ChevronRight, File, Folder } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { message } from './api'
import { Dialog } from './Dialog'
import { settingsRequest } from './settingsApi'

type Crumb = { name: string; path: string }
type Listing = {
  roots: string[]
  crumbs: Crumb[]
  entries: (Crumb & { directory: boolean })[]
  next?: string
  path?: string
}

export type Picker = { field: string; label: string; mode: string; value: string; select: (value: string) => void }

export function SettingsPicker({ picker, close }: { picker: Picker; close: () => void }) {
  const [listing, setListing] = useState<Listing>()
  const [input, setInput] = useState(picker.value)
  const [error, setError] = useState('')
  const [verified, setVerified] = useState<{ path: string; free_bytes?: number }>()
  const [busy, setBusy] = useState(false)
  const current = useRef(false)
  const file = picker.mode === 'file'

  useEffect(() => {
    let mounted = true
    settingsRequest<Listing>('/paths', { field: picker.field }).then((result) => {
      if (mounted) setListing((old) => old ?? result)
    }).catch((err: unknown) => {
      if (mounted) setError(message(err))
    })
    return () => { mounted = false }
  }, [picker.field])

  async function load(path = '', after = '') {
    if (current.current) return
    current.current = true
    setBusy(true)
    setError('')
    setVerified(undefined)
    try {
      const result = await settingsRequest<Listing>('/paths', { field: picker.field, path, after })
      setListing(after && listing ? { ...result, entries: [...listing.entries, ...result.entries] } : result)
      if (path && !file) setInput(result.path ?? path)
    } catch (err) {
      setError(message(err))
    } finally {
      current.current = false
      setBusy(false)
    }
  }

  async function check(select = false) {
    if (current.current) return
    current.current = true
    setBusy(true)
    setError('')
    setVerified(undefined)
    try {
      const result = await settingsRequest<{ path: string; free_bytes?: number }>(
        '/paths/check', { field: picker.field, path: input },
      )
      if (select) {
        picker.select(result.path)
        close()
      } else {
        setVerified(result)
      }
    } catch (err) {
      setError(message(err))
    } finally {
      current.current = false
      setBusy(false)
    }
  }

  return <Dialog title={file ? 'ファイルを選択' : 'フォルダを選択'} onClose={close}>
    <p>{picker.label}</p>
    <form onSubmit={(event) => { event.preventDefault(); void check() }}>
      <label className="settings-input-label">パスを入力
        <input autoFocus className="settings-input" type="text" value={input} onChange={(event) => {
          setInput(event.target.value)
          setVerified(undefined)
          setError('')
        }} />
      </label>
      <div className="settings-row">
        <button className="button secondary" disabled={busy || !input}>パスを検証</button>
        <button className="button secondary" type="button" disabled={busy} onClick={() => void load()}>許可ルートを表示</button>
      </div>
    </form>
    {listing && <div className="settings-listing" aria-label="フォルダの内容">
      <nav aria-label="パスのパンくず" className="settings-crumbs">
        <button type="button" className="button secondary" disabled={busy} onClick={() => void load()}>許可ルート</button>
        {listing.crumbs.map((crumb) => <button className="button secondary" type="button" key={crumb.path}
          disabled={busy} onClick={() => void load(crumb.path)}>
          <ChevronRight size={13} aria-hidden="true" />{crumb.name}
        </button>)}
      </nav>
      <ul>
        {listing.crumbs.length === 0 && listing.roots.map((root) => <li key={root}>
          <button type="button" className="button secondary" disabled={busy} onClick={() => void load(root)}>
            <Folder size={15} aria-hidden="true" />{root}
          </button>
        </li>)}
        {listing.entries.map((entry) => <li key={entry.path}>
          <button type="button" className="button secondary" disabled={busy} onClick={() => {
            if (entry.directory) void load(entry.path)
            else { setInput(entry.path); setVerified(undefined) }
          }}>
            {entry.directory ? <Folder size={15} aria-hidden="true" /> : <File size={15} aria-hidden="true" />}
            {entry.name}
          </button>
        </li>)}
      </ul>
      {listing.next && <button className="button secondary" type="button" disabled={busy}
        onClick={() => void load(listing.path, listing.next)}>さらに表示</button>}
      {!listing.next && !listing.entries.length && (listing.crumbs.length > 0 || !listing.roots.length) &&
        <p>表示できる項目がありません。</p>}
    </div>}
    {busy && <p role="status">確認しています…</p>}
    {error && <p role="alert" className="settings-error">{error}</p>}
    {verified && <p role="status">このパスを使用できます。
      {verified.free_bytes !== undefined && ` 空き容量 ${(verified.free_bytes / 2 ** 30).toFixed(1)} GiB`}
    </p>}
    <div className="dialog-actions">
      <button className="button secondary" type="button" onClick={close}>閉じる</button>
      <button className="button primary" type="button" disabled={busy || !input} onClick={() => void check(true)}>このパスを選択</button>
    </div>
  </Dialog>
}
