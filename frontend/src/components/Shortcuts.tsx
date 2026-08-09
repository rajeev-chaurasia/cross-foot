/** The keyboard contract, shown inline so a viewer sees it without asking. */

import { useEffect, useRef } from 'react'

import { BUTTON, KBD } from './ui'

interface Shortcut {
  keys: string
  description: string
}

const SHORTCUTS: readonly Shortcut[] = [
  { keys: 'j', description: 'Next field' },
  { keys: 'k', description: 'Previous field' },
  { keys: 'a', description: 'Accept the extracted value' },
  { keys: 'c', description: 'Correct the value' },
  { keys: 'Enter', description: 'Save the correction' },
  { keys: 'Esc', description: 'Leave the correction box' },
  { keys: '?', description: 'Show or hide this list' },
]

export function ShortcutsBar() {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
      {SHORTCUTS.map((shortcut) => (
        <li key={shortcut.keys} className="flex items-center gap-1.5">
          <kbd className={KBD}>{shortcut.keys}</kbd>
          <span>{shortcut.description}</span>
        </li>
      ))}
    </ul>
  )
}

interface OverlayProps {
  open: boolean
  onClose: () => void
}

export function ShortcutsOverlay({ open, onClose }: OverlayProps) {
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (open) {
      closeRef.current?.focus()
    }
  }, [open])

  if (!open) {
    return null
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Keyboard shortcuts"
        className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-5 shadow-lg"
      >
        <h2 className="text-lg font-semibold text-slate-900">Keyboard shortcuts</h2>
        <dl className="mt-3 space-y-2">
          {SHORTCUTS.map((shortcut) => (
            <div key={shortcut.keys} className="flex items-center gap-3">
              <dt>
                <kbd className={KBD}>{shortcut.keys}</kbd>
              </dt>
              <dd className="text-sm text-slate-700">{shortcut.description}</dd>
            </div>
          ))}
        </dl>
        <button ref={closeRef} type="button" className={`mt-4 ${BUTTON}`} onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  )
}
