import { useEffect } from 'react'

export function useShortcuts(handle: (key: string, event: KeyboardEvent) => void) {
  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      const target = event.target
      if (
        event.defaultPrevented || event.isComposing || document.querySelector('dialog[open]') ||
        (target instanceof HTMLElement && (target.isContentEditable ||
          target.closest('input, select, textarea, [role="textbox"], [role="combobox"]')))
      ) return
      handle(event.key.toLowerCase(), event)
    }
    window.addEventListener('keydown', keydown)
    return () => window.removeEventListener('keydown', keydown)
  })
}
