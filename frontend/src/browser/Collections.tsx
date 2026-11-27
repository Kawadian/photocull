import { Folder, Pencil, Sparkles, Trash2, X } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { json, request, useRemote } from '../api'
import type { Library } from '../api'
import { albumFilters, albumUrl, archiveUrl, changeMembers, saveAlbum } from './collections'
import type { Album, AssetRef, BatchResult, CollectionText } from './collections'
import type { BrowserLocation } from './location'
import './collections.css'

export function CollectionDialog({ title, close, text, children }: {
  title: string; close: () => void; text: CollectionText; children: ReactNode
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const heading = useId()
  useEffect(() => {
    const modal = dialog.current!
    modal.showModal()
    return () => modal.close()
  }, [])
  return <dialog ref={dialog} className="pcb-collection-dialog" aria-labelledby={heading}
    onCancel={(event) => { event.preventDefault(); close() }}>
    <div className="pcb-collection-panel">
      <header><h2 id={heading}>{title}</h2>
        <button className="pcb-control" type="button" aria-label={text.close} onClick={close}><X size={18} /></button>
      </header>
      {children}
    </div>
  </dialog>
}

export function Albums({ albums, error, retry, retryLabel, text, open, edit, onDelete }: {
  albums?: Album[]; error?: string; retry: () => void; retryLabel: string; text: CollectionText
  open: (album: Album) => void; edit: (album: Album) => void; onDelete: (album: Album) => void
}) {
  return <main className="pcb-scroller pcb-album-index" aria-busy={!albums && !error}>
    {error && <div className="pcb-state" role="alert"><p>{text.error}</p>
      <button className="pcb-control" type="button" onClick={retry}>{retryLabel}</button></div>}
    {!albums && !error && <div className="pcb-state" role="status"><span className="pcb-loading" /></div>}
    {albums?.length === 0 && <div className="pcb-state"><Folder size={28} /><p>{text.empty}</p></div>}
    {!!albums?.length && <ul className="pcb-album-list">
      {albums.map((album) => <li key={album.id}>
        <button type="button" className="pcb-album-open" onClick={() => open(album)}>
          {album.kind === 'manual' ? <Folder size={25} aria-hidden="true" /> : <Sparkles size={25} aria-hidden="true" />}
          <span><strong>{album.name}</strong>
            <small>{album.kind === 'manual' ? text.count(album.count ?? 0) : text.smart}</small></span>
        </button>
        <button type="button" className="pcb-control" title={text.edit}
          aria-label={text.edit + ' ' + album.name} onClick={() => edit(album)}><Pencil size={17} /></button>
        <button type="button" className="pcb-control" title={text.delete}
          aria-label={text.delete + ' ' + album.name} onClick={() => onDelete(album)}><Trash2 size={17} /></button>
      </li>)}
    </ul>}
  </main>
}

export function AlbumEditor({ album, location, libraries, allLibraries, libraryLabel, text, close, saved }: {
  album?: Album; location: BrowserLocation; libraries: Library[]; allLibraries: string; libraryLabel: string
  text: CollectionText; close: () => void; saved: (album: Album) => void
}) {
  const [name, setName] = useState(album?.name ?? '')
  const [kind, setKind] = useState<Album['kind']>(album?.kind ?? (Object.keys(location.filters).length ? 'smart' : 'manual'))
  const [library, setLibrary] = useState(album ? album.library ?? '' : location.library)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const filters = album ? albumFilters(album) : location.filters
  return <CollectionDialog text={text} title={album ? text.edit : text.new} close={close}>
    <form onSubmit={(event) => {
      event.preventDefault()
      setError(false)
      setBusy(true)
      void saveAlbum(name, kind, kind === 'smart' ? library : undefined, filters, album)
        .then(saved).catch(() => setError(true)).finally(() => setBusy(false))
    }}>
      <label>{text.name}<input maxLength={80} required value={name} onChange={(event) => setName(event.target.value)} /></label>
      {!album && <label>{text.type}<select value={kind} onChange={(event) => setKind(event.target.value as Album['kind'])}>
        <option value="manual">{text.manual}</option><option value="smart">{text.smart}</option>
      </select></label>}
      {kind === 'smart' && <>
        <label>{libraryLabel}<select value={library} onChange={(event) => setLibrary(event.target.value)}>
          <option value="">{allLibraries}</option>
          {libraries.map(({ name }) => <option key={name} value={name}>{name}</option>)}
        </select></label>
        <p>{text.smartHelp(Object.keys(filters).length)}</p>
      </>}
      {error && <p role="alert">{text.error}</p>}
      <footer><button type="button" className="pcb-control" onClick={close}>{text.cancel}</button>
        <button type="submit" className="pcb-control pcb-apply" disabled={busy || !name.trim()}>{text.save}</button></footer>
    </form>
  </CollectionDialog>
}

export function DeleteAlbum({ album, text, close, deleted }: {
  album: Album; text: CollectionText; close: () => void; deleted: () => void
}) {
  const [error, setError] = useState(false)
  const [busy, setBusy] = useState(false)
  return <CollectionDialog title={text.delete} text={text} close={close}>
    <p>{text.confirmDelete(album.name)}</p>
    {error && <p role="alert">{text.error}</p>}
    <footer><button type="button" className="pcb-control" onClick={close}>{text.cancel}</button>
      <button type="button" className="pcb-control pcb-danger" disabled={busy} onClick={() => {
        setBusy(true)
        setError(false)
        void fetch(albumUrl + '/' + album.id + '?revision=' + album.revision, { method: 'DELETE' })
          .then((response) => { if (response.ok) deleted(); else setError(true) })
          .catch(() => setError(true)).finally(() => setBusy(false))
      }}>{text.delete}</button></footer>
  </CollectionDialog>
}

type Action = 'add' | 'remove' | 'rating' | 'favorite' | 'addTag' | 'removeTag' | 'export'

export function SelectionActions({ assets, current, text, close, updated }: {
  assets: AssetRef[]; current?: Album; text: CollectionText; close: () => void
  updated: (remaining: Set<string>) => void
}) {
  const [action, setAction] = useState<Action>('add')
  const [destination, setDestination] = useState('')
  const [rating, setRating] = useState('3')
  const [favorite, setFavorite] = useState('true')
  const [tag, setTag] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const [failures, setFailures] = useState<BatchResult[]>([])
  const [retry, setRetry] = useState(0)
  const result = useRemote<{ items: Album[] }>(action === 'add' ? albumUrl : null, retry)
  const manual = result?.data?.items.filter((album) => album.kind === 'manual') ?? []
  const tagAction = action === 'addTag' || action === 'removeTag'
  const exportGroups = new Map<string, number[]>()
  for (const asset of assets) {
    const group = exportGroups.get(asset.library) ?? []
    group.push(asset.id)
    exportGroups.set(asset.library, group)
  }

  async function apply() {
    setBusy(true)
    setError(false)
    setFailures([])
    try {
      if (action === 'add' || action === 'remove') {
        const album = action === 'add' ? destination : current?.id
        if (!album) return
        await changeMembers(album, assets, action)
        updated(new Set())
      } else {
        const { items } = await request<{ items: BatchResult[] }>('/api/browse/selection/labels', json('PATCH', {
          assets, ...(action === 'rating' && { rating: Number(rating) }),
          ...(action === 'favorite' && { favorite: favorite === 'true' }),
          ...(action === 'addTag' && { add_tags: [tag.trim()] }),
          ...(action === 'removeTag' && { remove_tags: [tag.trim()] }),
        }))
        const failed = items.filter((item) => item.status !== 'done')
        setFailures(failed)
        updated(new Set(failed.map((item) => item.library)))
      }
    } catch {
      setError(true)
    } finally {
      setBusy(false)
    }
  }

  return <CollectionDialog title={text.actions} text={text} close={close}>
    <form onSubmit={(event) => { event.preventDefault(); void apply() }}>
      <label>{text.action}<select value={action} disabled={busy} onChange={(event) => {
        setAction(event.target.value as Action)
        setError(false)
        setFailures([])
      }}>
        <option value="add">{text.add}</option>
        {current?.kind === 'manual' && <option value="remove">{text.remove}</option>}
        <option value="rating">{text.rating}</option><option value="favorite">{text.favorite}</option>
        <option value="addTag">{text.addTag}</option><option value="removeTag">{text.removeTag}</option>
        <option value="export">{text.export}</option>
      </select></label>
      {action === 'add' && (result?.error
        ? <button type="button" className="pcb-control" onClick={() => setRetry((value) => value + 1)}>{text.error}</button>
        : result?.data && !manual.length ? <p>{text.noManual}</p>
        : <label>{text.chooseAlbum}<select required value={destination} onChange={(event) => setDestination(event.target.value)}>
          <option value="">{text.chooseAlbum}</option>
          {manual.map((album) => <option key={album.id} value={album.id}>{album.name}</option>)}
        </select></label>)}
      {action === 'rating' && <label>{text.rating}<select value={rating} onChange={(event) => setRating(event.target.value)}>
        {[0, 1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}
      </select></label>}
      {action === 'favorite' && <label>{text.favorite}<select value={favorite} onChange={(event) => setFavorite(event.target.value)}>
        <option value="true">{text.favoriteOn}</option><option value="false">{text.favoriteOff}</option>
      </select></label>}
      {tagAction && <label>{text.tag}<input maxLength={64} value={tag} required onChange={(event) => setTag(event.target.value)} /></label>}
      {action === 'export' && <div>
        <p>{text.perLibrary}</p>
        <ul className="pcb-export-groups">
          {[...exportGroups].map(([library, ids]) => <li key={library}>
            <a className="pcb-control" href={archiveUrl(library, ids)}>{library} · {text.count(ids.length)}</a>
          </li>)}
        </ul>
      </div>}
      {error && <p role="alert">{text.error}</p>}
      {!!failures.length && <div role="alert"><p>{text.failedLibraries}</p><ul>
        {failures.map(({ library, status }) => status !== 'done' && <li key={library}>{library} · {text[status]}</li>)}
      </ul></div>}
      <footer><button type="button" className="pcb-control" onClick={close}>{text.close}</button>
        {action !== 'export' && <button type="submit" className="pcb-control pcb-apply"
          disabled={busy || (action === 'add' && !destination) || (tagAction && !tag.trim())}>{text.apply}</button>}</footer>
    </form>
  </CollectionDialog>
}
