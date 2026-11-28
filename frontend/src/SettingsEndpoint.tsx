import { useRef, useState } from 'react'
import { message } from './api'
import { Dialog } from './Dialog'
import { settingsRequest } from './settingsApi'

type Model = {
  id: string
  vision: boolean | null
  pricing: Partial<Record<'prompt' | 'completion' | 'image' | 'request', string>>
}

const priceLabels = { prompt: '入力トークン', completion: '出力トークン', image: '画像', request: 'リクエスト' }

export function SettingsEndpoint({ baseUrl, model, locked, disabled, onSelect }: {
  baseUrl: string
  model: string
  locked: boolean
  disabled: boolean
  onSelect: (id: string) => void
}) {
  const [models, setModels] = useState<Model[]>()
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const active = useRef(false)

  async function probe(list: boolean) {
    if (active.current) return
    active.current = true
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const body = { base_url: baseUrl, model }
      if (list) {
        const result = await settingsRequest<{ items: Model[] }>('/models', body)
        setFilter('')
        setModels(result.items)
      } else {
        const { ok } = await settingsRequest<{ ok: boolean }>('/test', body)
        if (ok) setNotice('接続テストに成功しました。')
        else setError('接続テストに失敗しました。URL、モデル、キー、リクエスト上限を確認してください。')
      }
    } catch (err) {
      setError(message(err))
    } finally {
      active.current = false
      setBusy(false)
    }
  }

  const matching = models?.filter((item) => item.id.toLowerCase().includes(filter.trim().toLowerCase()))
  return <div className="settings-endpoint">
    <div className="settings-row">
      <button className="button secondary" type="button" disabled={busy || disabled || !baseUrl.trim()}
        onClick={() => void probe(true)}>モデル一覧を取得</button>
      <button className="button secondary" type="button" disabled={busy || disabled || !baseUrl.trim() || !model.trim()}
        onClick={() => void probe(false)}>接続テスト</button>
    </div>
    {busy && <p role="status">エンドポイントに接続しています…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert" className="settings-error">{error}</p>}
    {matching && <Dialog title="モデル一覧" onClose={() => setModels(undefined)}>
      <label className="settings-input-label">モデルを検索
        <input className="settings-input" autoFocus type="search" value={filter}
          onChange={(event) => setFilter(event.target.value)} />
      </label>
      <p>{matching.length} 件{matching.length > 100 && '（先頭 100 件を表示）'}</p>
      <ul className="settings-models">
        {matching.slice(0, 100).map((item) => <li key={item.id}>
          <button className="button secondary" type="button" disabled={locked} onClick={() => {
            onSelect(item.id)
            setModels(undefined)
          }}>{item.id}</button>
          <small>{item.vision === null ? '画像対応は不明' : item.vision ? '画像入力対応' : '画像入力非対応'}</small>
          {Object.entries(item.pricing).map(([kind, price]) => <small key={kind}>
            {priceLabels[kind as keyof typeof priceLabels]}あたり {price} USD
          </small>)}
        </li>)}
      </ul>
      <div className="dialog-actions">
        <button className="button secondary" type="button" onClick={() => setModels(undefined)}>閉じる</button>
      </div>
    </Dialog>}
  </div>
}
