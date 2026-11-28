import { Aperture, History, Search, Settings2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError, message } from './api'
import { Dialog } from './Dialog'
import { formatValue, settingsRequest } from './settingsApi'
import type { Changes, Diff, Settings } from './settingsApi'
import { SettingsEndpoint } from './SettingsEndpoint'
import { categories, descriptions } from './settingsFields'
import { inputProblem, SettingInput } from './SettingsInputs'
import { SettingsPicker } from './SettingsPicker'
import type { Picker } from './SettingsPicker'
import './settings.css'

type Category = typeof categories[number]
type Preview = Diff & { backup?: string }

const labels = new Map(descriptions.map(({ key, label }) => [key, label]))

function timestamp(revision: string) {
  const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/.exec(revision)
  if (!match) return revision
  const [, year, month, day, hour, minute, second] = match
  return `${year}-${month}-${day} ${hour}:${minute}:${second} UTC`
}

function Reapply({ managed }: { managed: boolean }) {
  return <details className="settings-apply">
    <summary>保存した設定を反映する</summary>
    <p>配備に使った Compose ファイルとプロファイルを指定して、API とワーカーを再起動してください。</p>
    <code>docker compose restart api worker</code>
    {managed && <p>egress の許可ホストを変更した場合は、同じ Compose 構成で <code>docker compose restart egress-proxy</code> を実行します。</p>}
    <p>環境変数やマウントを変更した場合は、同じ構成で <code>docker compose up -d</code> を実行します。
      systemd では <code>systemctl restart photocull-api photocull-worker</code> を実行します。</p>
  </details>
}

function SettingsEditor({ initial }: { initial: Settings }) {
  const [saved, setSaved] = useState(initial)
  const [changes, setChanges] = useState<Changes>({})
  const [category, setCategory] = useState<Category>(categories[0])
  const [query, setQuery] = useState('')
  const [picker, setPicker] = useState<Picker>()
  const [preview, setPreview] = useState<Preview>()
  const [showHistory, setShowHistory] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const active = useRef(false)

  const count = Object.keys(changes).length
  const pending = Object.values(saved.fields).filter((field) => field.pending).length
  const search = query.trim().toLowerCase()
  const result = descriptions.filter((entry) => saved.fields[entry.key] && (
    search ? [entry.label, entry.help, entry.key, entry.category, saved.fields[entry.key].source]
      .join(' ').toLowerCase().includes(search) : entry.category === category
  ))

  useEffect(() => {
    if (!count) return
    function prevent(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', prevent)
    return () => window.removeEventListener('beforeunload', prevent)
  }, [count])

  function change(key: string, value: unknown) {
    setError('')
    setNotice('')
    setChanges((previous) => {
      const next = { ...previous }
      if (JSON.stringify(value) === JSON.stringify(saved.fields[key].value)) delete next[key]
      else next[key] = value
      return next
    })
  }

  function value(key: string) {
    return Object.hasOwn(changes, key) ? changes[key] : saved.fields[key]?.value
  }

  async function apply(save: boolean, backup?: string) {
    if (active.current) return
    const invalid = Object.entries(changes).find(([key, item]) => inputProblem(key, saved.fields[key], item))
    if (invalid && !backup) {
      setError(`${labels.get(invalid[0])}の入力を確認してください。`)
      return
    }
    active.current = true
    setBusy(true)
    setError('')
    setNotice('')
    const revision = saved.revision
    try {
      if (save) {
        const next = backup
          ? await settingsRequest<Settings>('/history/restore', { revision, backup })
          : await settingsRequest<Settings>('', { revision, changes }, 'PUT')
        setSaved(next)
        setChanges({})
        setPreview(undefined)
        setShowHistory(false)
        setNotice(backup ? '以前の版を復元しました。' : '設定を保存しました。')
      } else {
        const next = backup
          ? await settingsRequest<Diff>('/history/preview', { revision, backup })
          : await settingsRequest<Diff>('/preview', { revision, changes })
        setShowHistory(false)
        setPreview({ ...next, backup })
      }
    } catch (err) {
      setError(message(err))
    } finally {
      active.current = false
      setBusy(false)
    }
  }

  function row(entry: typeof descriptions[number]) {
    const { key, label, help } = entry
    const field = saved.fields[key]
    const current = value(key)
    const env = field.source.startsWith('PHOTOCULL_')
    const dirty = Object.hasOwn(changes, key)
    const problem = dirty && inputProblem(key, field, current)
    return <section className="settings-field" aria-label={label} key={key}>
      <div className="settings-field-header">
        <div><h3>{label}</h3><code>{key}</code></div>
        <span className="settings-restart">{field.restart === 'egress-proxy' ? 'egress 再起動が必要' : '再起動が必要'}</span>
      </div>
      <p>{help}</p>
      {env && <p className="settings-source">env で上書き中 · {field.source}</p>}
      <SettingInput field={key} label={label} schema={field} value={current} disabled={env || busy}
        change={(next) => change(key, next)} pick={setPicker} lockedLibraries={saved.locked_libraries} />
      {problem && <p className="settings-error">{problem}</p>}
      {field.pending && <p>稼働中の API の値 <code>{formatValue(field.running)}</code></p>}
      {dirty && <p>保存済みの値 <code>{formatValue(field.value)}</code></p>}
      <div className="settings-default">
        <small>既定値 <code>{formatValue(field.default)}</code></small>
        <button type="button" className="button secondary" disabled={env || busy || (
          JSON.stringify(current) === JSON.stringify(field.default) && key !== 'libraries'
        )} onClick={() => {
          if (key !== 'libraries') change(key, field.default)
          else change(key, Object.fromEntries(Object.entries(field.value as Record<string, unknown>)
            .filter(([name]) => Object.hasOwn(saved.locked_libraries, name))))
        }}>既定値に戻す</button>
      </div>
    </section>
  }

  return <div className="settings-page">
    <div className="settings-heading">
      <div><h1>設定</h1><p>Photocull の動作と接続先を管理します。</p></div>
      <button className="button secondary" type="button" disabled={busy} onClick={() => {
        setError('')
        setShowHistory(true)
      }}><History size={17} aria-hidden="true" />変更履歴</button>
    </div>
    <label className="settings-search">
      <Search size={18} aria-hidden="true" />
      <input className="settings-input" type="search" aria-label="設定を検索" placeholder="すべての設定を検索"
        value={query} onChange={(event) => setQuery(event.target.value)} />
    </label>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="設定カテゴリ">
        {categories.map((item) => <button
          className="button secondary" type="button" key={item} aria-current={!search && item === category ? 'page' : undefined}
          onClick={() => { setCategory(item); setQuery('') }}>{item}</button>)}
      </nav>
      <label className="settings-mobile-nav settings-input-label">設定カテゴリ
        <select className="settings-input" value={category} onChange={(event) => {
          setCategory(event.target.value as Category)
          setQuery('')
        }}>{categories.map((item) => <option key={item}>{item}</option>)}</select>
      </label>
      <main className="settings-content">
        <h2>{search ? `検索結果（${result.length} 件）` : category}</h2>
        {result.length === 0 && <p>該当する設定はありません。</p>}
        {!search && category === '判定プロバイダー' && <p className="settings-info">
          API キーは{saved.api_key_configured ? '設定済み' : '未設定'}です。環境変数で管理します。
        </p>}
        {!search && category === 'ネットワーク・egress' && <div className="settings-info">
          <p>ワーカーネットワーク <code>{saved.network.worker}</code> · プロキシ <code>{saved.network.proxy}</code></p>
          <p>ネットワークの切り替えは Compose 側で行います。<code>docker compose up -d</code> で反映してください。</p>
          {!saved.network.proxy_reads_saved_settings &&
            <p>保存した許可ホストを使うには、<code>deploy/compose.settings-egress.yml</code> を追加してください。</p>}
        </div>}
        {result.filter((entry) => search || !entry.advanced).map((entry) => <div key={entry.key}>
          {row(entry)}
          {entry.key === 'vlm.openai.model' && <SettingsEndpoint
            baseUrl={String(value('vlm.openai.base_url') ?? '')}
            model={String(value(entry.key) ?? '')}
            locked={saved.fields[entry.key].source.startsWith('PHOTOCULL_')}
            disabled={busy} onSelect={(id) => change(entry.key, id)} />}
        </div>)}
        {!search && result.some((entry) => entry.advanced) &&
          <details key={category} className="settings-advanced">
            <summary>詳細設定</summary>
            {result.filter((entry) => entry.advanced).map(row)}
          </details>}
        {saved.warnings.map((warning) => <p key={warning} className="settings-warning">{warning}</p>)}
        <Reapply managed={saved.network.proxy_reads_saved_settings} />
      </main>
    </div>
    <div className="settings-savebar">
      <div>
        <div role="status">{busy ? '処理しています…' : notice || (count ? `未保存の変更 ${count} 件` : '未保存の変更はありません')}</div>
        {pending > 0 && <small>API に未反映の設定 {pending} 件</small>}
        {error && !preview && !showHistory && <p role="alert" className="settings-error">{error}</p>}
      </div>
      <div className="settings-row">
        <button className="button secondary" type="button" disabled={!count || busy} onClick={() => {
          setChanges({})
          setError('')
          setNotice('変更を取り消しました。')
        }}>取り消し</button>
        <button className="button primary" type="button" disabled={!count || busy} onClick={() => void apply(false)}>差分を確認</button>
      </div>
    </div>
    {picker && <SettingsPicker picker={picker} close={() => setPicker(undefined)} />}
    {showHistory && <Dialog title="変更履歴" onClose={() => setShowHistory(false)} busy={busy}>
      {count > 0 && <p>履歴を復元する前に、未保存の変更を保存または取り消してください。</p>}
      {saved.history.length === 0 && <p>まだ変更履歴はありません。</p>}
      <ul className="settings-history">
        {saved.history.map((backup) => <li key={backup}>
          <time>{timestamp(backup)}</time>
          <button className="button secondary" type="button" disabled={busy || count > 0}
            onClick={() => void apply(false, backup)}>復元差分を確認</button>
        </li>)}
      </ul>
      {error && <p role="alert" className="settings-error">{error}</p>}
      <div className="dialog-actions">
        <button className="button secondary" type="button" disabled={busy} onClick={() => setShowHistory(false)}>閉じる</button>
      </div>
    </Dialog>}
    {preview && <Dialog title={preview.backup ? '復元の差分' : '保存前の差分'} onClose={() => setPreview(undefined)} busy={busy}>
      {preview.changes.length === 0 && <p>有効な値の差分はありません。</p>}
      <ul className="settings-diff">
        {preview.changes.map((item) => <li key={item.key}>
          <strong>{labels.get(item.key) ?? item.key}</strong>
          <div><span>変更前</span><pre>{formatValue(item.before)}</pre></div>
          <div><span>変更後</span><pre>{formatValue(item.after)}</pre></div>
        </li>)}
      </ul>
      {preview.warnings.map((warning) => <p key={warning} className="settings-warning">{warning}</p>)}
      {error && <p role="alert" className="settings-error">{error}</p>}
      <div className="dialog-actions">
        <button className="button secondary" type="button" disabled={busy} onClick={() => setPreview(undefined)}>戻る</button>
        <button className="button primary" type="button" disabled={busy} onClick={() => void apply(true, preview.backup)}>
          {preview.backup ? 'この版を復元' : '設定を保存'}
        </button>
      </div>
    </Dialog>}
  </div>
}

export default function SettingsScreen() {
  const [state, setState] = useState<{ data?: Settings; error?: string }>()
  useEffect(() => {
    let active = true
    settingsRequest<Settings>().then((data) => {
      if (active) setState({ data })
    }).catch((err: unknown) => {
      if (!active) return
      const denied = err instanceof ApiError && [401, 403].includes(err.status)
      setState({ error: denied ? '設定画面には管理者認証が必要です。トークンを設定した管理者用のアクセス経路から開いてください。' : message(err) })
    })
    return () => { active = false }
  }, [])
  return <div className="settings-shell" lang="ja">
    <header className="app-header">
      <div className="wordmark"><Aperture size={24} strokeWidth={1.9} /><span>Photocull</span></div>
      <nav className="header-actions" aria-label="Photocull">
        <a className="button secondary" href="/">レビュー</a>
        <a className="button secondary" href="/?view=browser">写真ブラウザ</a>
        <span className="settings-nav-label" aria-current="page"><Settings2 size={17} aria-hidden="true" />設定</span>
      </nav>
    </header>
    {state?.data ? <SettingsEditor initial={state.data} /> :
      <main className="settings-page">
        <h1>設定</h1>
        {state?.error ? <p className="settings-error" role="alert">{state.error}</p> : <p role="status">設定を読み込んでいます…</p>}
      </main>}
  </div>
}
