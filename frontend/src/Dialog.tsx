import { useEffect, useId, useRef } from 'react'
import type { ReactNode } from 'react'

export function Dialog({ title, children, onClose, busy = false }: {
  title: string
  children: ReactNode
  onClose: () => void
  busy?: boolean
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const element = dialog.current
    element?.showModal()
    return () => element?.close()
  }, [])
  return (
    <dialog className="modal" ref={dialog} aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault()
        if (!busy) onClose()
      }}>
      <h2 id={titleId}>{title}</h2>
      {children}
    </dialog>
  )
}

const reviewKeys = [
  ['← / →', 'Previous or next photo'],
  ['1–5', 'Rate the current photo'],
  ['X / 0', 'Reject or clear the rating'],
  ['U / Ctrl+Z / ⌘Z', 'Undo the last rating'],
  ['Z', 'Toggle fit and 100% zoom'],
]

const burstKeys = [
  ['[ / ]', 'Previous or next burst'],
  ['B', 'Set the current photo as best'],
  ['R', 'Open the reject-rest confirmation'],
  ['Shift+U', 'Undo the last burst rejection'],
]

const searchKeys = [
  ['/', 'Focus photo search'],
  ['Enter', 'Submit the search'],
  ['Esc', 'Leave the search field or close results'],
]

const insightKeys = [
  ['I', 'Switch between photo review and insights'],
  ['Esc', 'Return to photo review'],
  ['← / →', 'Previous or next clip in the Videos view'],
]

export function ShortcutHelp({ onClose }: { onClose: () => void }) {
  const sections = [['Photo review', reviewKeys], ['Burst review', burstKeys], ['Photo search', searchKeys], ['Insights', insightKeys]] as const
  return (
    <Dialog title="Keyboard shortcuts" onClose={onClose}>
      {sections.map(([section, keys]) => (
        <section className="shortcut-section" key={section}>
          <h3>{section}</h3>
          <dl>
            {keys.map(([key, action]) => (
              <div key={key}><dt><kbd>{key}</kbd></dt><dd>{action}</dd></div>
            ))}
          </dl>
        </section>
      ))}
      <div className="dialog-actions">
        <button autoFocus className="button secondary" onClick={onClose}>Close</button>
      </div>
    </Dialog>
  )
}
