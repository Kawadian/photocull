import { useEffect, useState } from 'react'
import { ApiError, message } from './api'
import { settingsRequest } from './settingsApi'
import type { RestartInfo, Settings } from './settingsApi'

const pollDelay = 1500
const acknowledgementLimit = 20_000
const reconnectLimit = 610_000

function manualCommands(info: RestartInfo, managedEgress: boolean) {
  const appliesEgress = info.services.includes('egress-proxy') && managedEgress
  const compose = info.services.filter((service) => service !== 'egress-proxy' || appliesEgress)
  return <>
    {compose.length > 0 && <p>配備に使ったファイルとプロファイルを指定して <code>docker compose restart {compose.join(' ')}</code> を実行してください。</p>}
    {info.services.includes('worker') && <p>systemd 配備では <code>systemctl restart photocull-worker photocull-api</code> を実行してください。</p>}
    {info.services.includes('egress-proxy') && !managedEgress && <p>
      egress の反映には <code>deploy/compose.settings-egress.yml</code> を追加して <code>docker compose up -d</code> を実行してください。
    </p>}
  </>
}

export function SettingsApply({ info, revision, managedEgress, disabled, refresh, update }: {
  info: RestartInfo
  revision: string
  managedEgress: boolean
  disabled: boolean
  refresh: (settings: Settings) => void
  update: (info: RestartInfo) => void
}) {
  const [attempt, setAttempt] = useState<{ previous?: string; started: number }>()
  const [error, setError] = useState('')
  const [offline, setOffline] = useState(false)
  const [unknown, setUnknown] = useState(false)
  const polling = !unknown && (Boolean(attempt) || info.busy)

  useEffect(() => {
    if (!polling) return
    let cancelled = false
    let timer: number
    const started = attempt?.started ?? Date.now()

    async function tick() {
      if (cancelled) return
      if (Date.now() - started > reconnectLimit) {
        setAttempt(undefined)
        setUnknown(true)
        setOffline(false)
        setError('再起動結果を確認できませんでした。ホストの状態を確認し、設定画面を再読み込みしてください。')
        return
      }
      try {
        const next = await settingsRequest<RestartInfo>('/restart')
        if (cancelled) return
        const seen = !attempt || next.result?.request.id !== attempt.previous && next.result !== null
        if (seen && !next.busy) {
          const settings = await settingsRequest<Settings>()
          if (cancelled) return
          refresh(settings)
          setAttempt(undefined)
          setOffline(false)
          return
        }
        if (seen) {
          update(next)
        } else if (Date.now() - started > acknowledgementLimit) {
          setAttempt(undefined)
          setError('再起動の要求を確認できませんでした。ホストの状態を確認してから、もう一度お試しください。')
          setOffline(false)
          return
        }
        setOffline(false)
      } catch {
        if (cancelled) return
        setOffline(true)
      }
      timer = window.setTimeout(tick, pollDelay)
    }
    timer = window.setTimeout(tick, pollDelay)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [attempt, polling, refresh, update])

  async function apply() {
    setError('')
    setUnknown(false)
    setAttempt({ previous: info.result?.request.id, started: Date.now() })
    try {
      update(await settingsRequest<RestartInfo>('/restart', { revision }))
    } catch (err) {
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        setAttempt(undefined)
        setError(message(err))
      } else {
        // The API may restart after accepting the handoff but before returning 202.
        setOffline(true)
      }
    }
  }

  const result = info.result?.request.revision === revision || polling ? info.result : null
  if (!info.services.length && !result && !error) return null
  return <section className="settings-apply-banner" aria-label="設定の適用">
    {info.services.length > 0 && <h2>再起動が必要な変更があります</h2>}
    {polling && <p role="status">{offline ? 'API への再接続を待っています…' :
      result?.status === 'running' ? 'ホストでサービスを再起動しています…' : 'ホストの再起動処理を待っています…'}</p>}
    {!polling && result?.status === 'succeeded' && <p role="status">設定の適用が完了しました。</p>}
    {!polling && result?.status === 'failed' && <p role="alert" className="settings-error">
      再起動に失敗しました。ホストで再起動サービスの状態を確認してください。
      {result.restarted.length > 0 && ` 再起動済み ${result.restarted.join(', ')}`}
    </p>}
    {!polling && result?.status === 'expired' && <p role="alert" className="settings-error">ホストから再起動の結果が返りませんでした。</p>}
    {error && <p role="alert" className="settings-error">{error}</p>}
    {info.services.length > 0 && (info.automatic && !unknown ?
      <button className="button primary" type="button" disabled={disabled || polling} onClick={() => void apply()}>
        適用（再起動）
      </button> : manualCommands(info, managedEgress))}
  </section>
}
