import { Copy, Download, Link, MapPin, MapPinOff, X } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import type { Language } from './location'
import type { BrowserMedia } from './ResponsiveImage'
import './delivery.css'

const en = {
  title: 'Share or download', close: 'Close sharing and downloads', action: 'Action',
  share: 'Share a link', download: 'Download a copy', location: 'Location information',
  exclude: 'Exclude location information', include: 'Include location information',
  shareHelp: 'Anyone opening the link needs access to this Photocull instance.',
  prepare: 'Prepare link', prepareDownload: 'Prepare download', retry: 'Try again',
  waiting: 'Preparing media…', failed: 'Could not prepare this media. Try again.',
  link: 'Share link', copy: 'Copy link', copied: 'Link copied.',
  copyFailed: 'Select and copy the link from the field.',
  original: 'Download original', originalHelp: 'Original file. May contain location and other camera metadata.',
}
const ja: typeof en = {
  title: '共有・ダウンロード', close: '共有・ダウンロードを閉じる', action: '操作',
  share: 'リンクを共有', download: 'コピーをダウンロード', location: '位置情報',
  exclude: '位置情報を含めない', include: '位置情報を含める',
  shareHelp: 'リンクを開くには、この Photocull へのアクセスが必要です。',
  prepare: 'リンクを準備', prepareDownload: 'ダウンロードを準備', retry: '再試行',
  waiting: 'ファイルを準備中…', failed: 'ファイルを準備できませんでした。再試行してください。',
  link: '共有リンク', copy: 'リンクをコピー', copied: 'リンクをコピーしました。',
  copyFailed: '入力欄のリンクを選択してコピーしてください。',
  original: '原本をダウンロード', originalHelp: '原本には位置情報やカメラのメタデータが含まれることがあります。',
}
export const deliveryText = { en, ja }

type Action = 'share' | 'download'

export function Delivery({ lang, assetUrl, hash, media, close }: {
  lang: Language; assetUrl: string; hash: string; media: BrowserMedia; close: () => void
}) {
  const text = deliveryText[lang]
  const title = useId()
  const originalHelp = useId()
  const modal = useRef<HTMLDialogElement>(null)
  const [action, setAction] = useState<Action>('share')
  const [include, setInclude] = useState(media.strip_location === false)
  const [preparing, setPreparing] = useState<{ path: string }>()
  const [status, setStatus] = useState<'waiting' | 'ready' | 'failed' | undefined>()
  const [copyResult, setCopyResult] = useState('')
  const path = assetUrl + '/delivery/' + hash + '-' + media.recipe + '?'
    + new URLSearchParams({ action, include_location: String(include) })
  const rendered = !!media.recipe && /^[a-f0-9]{64}$/.test(hash)

  useEffect(() => {
    const dialog = modal.current!
    dialog.showModal()
    return () => dialog.close()
  }, [])

  useEffect(() => {
    if (!preparing) return
    const controller = new AbortController()
    let timer = 0
    const deadline = Date.now() + 120_000
    async function poll(url: string) {
      try {
        const response = await fetch(url, { method: 'HEAD', cache: 'no-store', signal: controller.signal })
        if (controller.signal.aborted) return
        if (response.status === 200) setStatus('ready')
        else if ([202, 503].includes(response.status) && Date.now() < deadline) {
          const delay = Number(response.headers.get('Retry-After') ?? '2')
          timer = window.setTimeout(() => { void poll(url) },
            Number.isFinite(delay) ? Math.min(10, Math.max(1, delay)) * 1000 : 2000)
        } else setStatus('failed')
      } catch {
        if (!controller.signal.aborted) setStatus('failed')
      }
    }
    void poll(preparing.path)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [preparing])

  function clear() {
    setPreparing(undefined)
    setStatus(undefined)
    setCopyResult('')
  }

  const ready = status === 'ready' && preparing?.path === path
  const share = new URL(path, window.location.href).toString()
  return <dialog className="pcb-delivery" aria-labelledby={title} ref={modal}
    onCancel={(event) => { event.preventDefault(); event.stopPropagation(); close() }}
    onKeyDown={(event) => event.stopPropagation()}>
    <header>
      <h2 id={title}>{text.title}</h2>
      <button type="button" className="pcb-icon" aria-label={text.close} title={text.close} onClick={close}>
        <X size={18} aria-hidden="true" />
      </button>
    </header>
    {rendered && <form onSubmit={(event) => {
      event.preventDefault()
      setPreparing({ path })
      setStatus('waiting')
    }}>
      <label>{text.action}<select value={action} onChange={(event) => {
        clear()
        setAction(event.target.value as Action)
        setInclude(media.strip_location === false)
      }}>
        <option value="share">{text.share}</option>
        <option value="download">{text.download}</option>
      </select></label>
      <fieldset>
        <legend>{text.location}</legend>
        <label><input type="radio" name="location" checked={!include} onChange={() => { clear(); setInclude(false) }} />
          <MapPinOff size={16} aria-hidden="true" />{text.exclude}</label>
        <label><input type="radio" name="location" checked={include} onChange={() => { clear(); setInclude(true) }} />
          <MapPin size={16} aria-hidden="true" />{text.include}</label>
      </fieldset>
      {action === 'share' && <p>{text.shareHelp}</p>}
      {status === 'waiting' && <p role="status">{text.waiting}</p>}
      {status === 'failed' && <p role="alert">{text.failed}</p>}
      {!ready && <button type="submit" className="pcb-control pcb-delivery-primary" disabled={status === 'waiting'}>
        {action === 'share' ? <Link size={16} aria-hidden="true" /> : <Download size={16} aria-hidden="true" />}
        {status === 'failed' ? text.retry : action === 'share' ? text.prepare : text.prepareDownload}
      </button>}
      {ready && action === 'share' && <>
        <label>{text.link}<input value={share} readOnly autoFocus onFocus={(event) => event.currentTarget.select()} /></label>
        <button type="button" className="pcb-control pcb-delivery-primary" onClick={() => {
          if (!navigator.clipboard) setCopyResult(text.copyFailed)
          else void navigator.clipboard.writeText(share)
            .then(() => setCopyResult(text.copied), () => setCopyResult(text.copyFailed))
        }}><Copy size={16} aria-hidden="true" />{text.copy}</button>
        {copyResult && <p role="status">{copyResult}</p>}
      </>}
      {ready && action === 'download' && <a className="pcb-control pcb-delivery-primary" href={path} download>
        <Download size={16} aria-hidden="true" />{text.download}
      </a>}
    </form>}
    {media.original_download && <section>
      <a className="pcb-control" href={assetUrl + '/original?include_location=true'} download aria-describedby={originalHelp}>
        <Download size={16} aria-hidden="true" />{text.original}
      </a>
      <p id={originalHelp}><MapPin size={16} aria-hidden="true" />{text.originalHelp}</p>
    </section>}
  </dialog>
}
