import { FolderOpen } from 'lucide-react'
import type { ReactNode } from 'react'
import type { Field } from './settingsApi'
import type { Picker } from './SettingsPicker'

type Props = {
  field: string
  label: string
  value: unknown
  schema: Field
  disabled: boolean
  change: (value: unknown) => void
  pick: (picker: Picker) => void
  lockedLibraries: Record<string, string>
}

type Library = { root: string; allowed_roots: string[] }

function addRow<T>(rows: Record<string, T>, value: T, change: (value: Record<string, T>) => void) {
  if (!Object.hasOwn(rows, '')) change({ ...rows, '': value })
}

function MapRows<T>({ rows, render, change, disabled, locked = {} }: {
  rows: Record<string, T>
  render: (value: T, update: (value: T) => void, locked: boolean) => ReactNode
  change: (value: Record<string, T>) => void
  disabled: boolean
  locked?: Record<string, string>
}) {
  const entries = Object.entries(rows)
  return entries.map(([name, item], index) => {
    const readonly = disabled || Object.hasOwn(locked, name)
    return <div className="settings-map" key={index}>
      <div className="settings-row">
        <label className="settings-input-label">名前
          <input className="settings-input" value={name} disabled={readonly}
            onChange={(event) => {
              const next = event.target.value
              if (next !== name && !Object.hasOwn(rows, next)) {
                change(Object.fromEntries(entries.map((entry, i) => i === index ? [next, item] : entry)))
              }
            }} />
        </label>
        <button className="button secondary" type="button" disabled={readonly}
          onClick={() => change(Object.fromEntries(entries.filter((_, i) => i !== index)))}>削除</button>
      </div>
      {locked[name] && <small>env で上書き中 · {locked[name]}</small>}
      {render(item, (value) => change({ ...rows, [name]: value }), readonly)}
    </div>
  })
}

const optionLabels: Record<string, string> = {
  local: 'ローカル', openai: 'OpenAI 互換', unjudged: '未判定', auto: '自動', poll: 'ポーリング',
}

export function SettingInput({ field, label, value, schema, change, disabled, pick, lockedLibraries }: Props) {
  function pathInput(title: string, path: string, set: (value: string) => void, readonly = disabled) {
    return <div className="settings-path">
      <input className="settings-input" aria-label={title} type="text" value={path} disabled={readonly}
        onChange={(event) => set(event.target.value)} spellCheck={false} />
      <button className="button secondary" type="button" disabled={readonly} aria-label={`${title}を参照`}
        onClick={() => pick({ field, label: title, mode: schema.path ?? 'directory', value: path, select: set })}>
        <FolderOpen size={16} aria-hidden="true" /><span>参照</span>
      </button>
    </div>
  }

  function list(items: string[], update: (value: string[]) => void, title: string, paths: boolean, readonly = disabled) {
    return <div className="settings-list-input">
      {items.map((item, index) => {
        const name = `${title} ${index + 1}`
        const set = (value: string) => update(items.map((entry, i) => i === index ? value : entry))
        return <div className="settings-path" key={index}>
          {paths ? pathInput(name, item, set, readonly) :
            <input className="settings-input" aria-label={name} type="text" value={item} disabled={readonly}
              onChange={(event) => set(event.target.value)} />}
          <button className="button secondary" type="button" aria-label={`${name}を削除`} disabled={readonly}
            onClick={() => update(items.filter((_, i) => i !== index))}>削除</button>
        </div>
      })}
      <button className="button secondary" type="button" disabled={readonly} onClick={() => update([...items, ''])}>
        {title}を追加
      </button>
    </div>
  }

  if (schema.path === 'libraries') {
    const rows = (value ?? {}) as Record<string, Library>
    return <div className="settings-collection">
      <MapRows rows={rows} disabled={disabled} locked={lockedLibraries} change={change}
        render={(item, update, readonly) => <>
          <label className="settings-input-label">原本のルート</label>
          {pathInput('原本のルート', item.root, (root) => update({ ...item, root }), readonly)}
          {list(item.allowed_roots, (allowed_roots) => update({ ...item, allowed_roots }), '追加の読み取りルート', true, readonly)}
        </>} />
      <button className="button secondary" type="button" disabled={disabled || Object.hasOwn(rows, '')}
        onClick={() => addRow(rows, { root: '', allowed_roots: [] }, change)}>ライブラリを追加</button>
    </div>
  }
  if (schema.path?.endsWith('-map')) {
    const rows = (value ?? {}) as Record<string, string>
    return <div className="settings-collection">
      <MapRows rows={rows} change={change} disabled={disabled}
        render={(path, set) => pathInput(label, path, set)} />
      <button className="button secondary" type="button" disabled={disabled || Object.hasOwn(rows, '')}
        onClick={() => addRow(rows, '', change)}>保存先を追加</button>
    </div>
  }
  if (field === 'ml.tag_vocabulary') {
    const rows = (value ?? {}) as Record<string, string[]>
    return <div className="settings-collection">
      <MapRows rows={rows} change={change} disabled={disabled}
        render={(names, set) => list(names, set, 'ラベル', false)} />
      <button className="button secondary" type="button" disabled={disabled || Object.hasOwn(rows, '')}
        onClick={() => addRow(rows, [''], change)}>タグを追加</button>
    </div>
  }
  if (schema.path === 'directories' || (schema.type === 'array' && field !== 'worker.stages')) {
    return list((value ?? []) as string[], change, schema.path ? 'ルート' : 'ホスト', Boolean(schema.path))
  }
  if (schema.path) {
    return pathInput(label, String(value ?? ''), (next) => change(next || (schema.nullable ? null : '')))
  }
  if (field === 'worker.stages') {
    const selected = value as string[]
    return <div className="settings-row">{['exif', 'preview', 'score'].map((stage) => <label key={stage}>
      <input type="checkbox" checked={selected.includes(stage)} disabled={disabled}
        onChange={(event) => change(event.target.checked ? [...selected, stage] : selected.filter((item) => item !== stage))} /> {stage}
    </label>)}</div>
  }
  if (schema.type === 'boolean') {
    return <select className="settings-input" aria-label={label} value={value ? 'true' : 'false'} disabled={disabled}
      onChange={(event) => change(event.target.value === 'true')}>
      <option value="true">有効</option><option value="false">無効</option>
    </select>
  }
  if (schema.enum) {
    return <select className="settings-input" aria-label={label} value={String(value)} disabled={disabled}
      onChange={(event) => change(event.target.value)}>
      {schema.enum.map((option) => <option key={option} value={option}>{optionLabels[option] ?? option}</option>)}
    </select>
  }
  const numeric = schema.type === 'number' || schema.type === 'integer'
  const wideInteger = field === 'insights.people_min_faces'
  return <input className="settings-input" aria-label={label} value={String(value ?? '')} disabled={disabled}
    type={numeric && !wideInteger ? 'number' : 'text'} inputMode={wideInteger ? 'numeric' : undefined}
    min={schema.minimum} max={schema.maximum} step={schema.multipleOf ?? (schema.type === 'integer' ? 1 : 'any')}
    maxLength={schema.maxLength} spellCheck={false}
    onChange={(event) => {
      const next = event.target.value
      change(numeric && !wideInteger && next !== '' ? event.target.valueAsNumber : next)
    }} />
}

export function inputProblem(key: string, schema: Field, value: unknown) {
  if (schema.type === 'integer' || schema.type === 'number') {
    if (key === 'insights.people_min_faces') {
      if (!/^\d+$/.test(String(value)) || BigInt(String(value)) < 1n || BigInt(String(value)) > 9223372036854775807n) return '1 以上の整数を範囲内で入力してください。'
    } else if (typeof value !== 'number' || !Number.isFinite(value) ||
      (schema.type === 'integer' && !Number.isInteger(value)) ||
      (schema.minimum !== undefined && value < schema.minimum) ||
      (schema.maximum !== undefined && value > schema.maximum) ||
      (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) ||
      (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) ||
      (schema.multipleOf !== undefined && value % schema.multipleOf !== 0)) {
      return '指定された範囲の数値を入力してください。'
    }
  }
  if (schema.pattern && typeof value === 'string' && !new RegExp(schema.pattern).test(value)) {
    return '指定の形式で入力してください。'
  }
}
