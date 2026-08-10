/**
 * What the last correction did, kept on screen until the next one.
 *
 * This is the payoff of the whole product and it is deliberately not a toast. A
 * reviewer holding j and a moves through a queue faster than any timed banner
 * survives, so the panel stays put and is simply replaced the next time a
 * correction is saved.
 *
 * It also has to be visible while the round trip is still going. Reconciling a
 * document again takes longer than writing the correction did, and a save that
 * shows nothing for two seconds reads as a save that did not happen, so the
 * panel says it is checking rather than staying blank.
 */

import type { CorrectionReconciliation } from '../api/types'
import { reconciliationHeadline, reconciliationMessage } from '../lib/reconciliation'
import { CARD } from './ui'

/** Mirrors the mutation's own status, so nothing here tracks state twice. */
export type CorrectionStatus = 'idle' | 'pending' | 'success' | 'error'

export interface CorrectionOutcomeProps {
  status: CorrectionStatus
  /** The field the correction was saved against, in words. */
  fieldLabel: string | null
  /** The value the reviewer typed. */
  value: string | null
  reconciliation: CorrectionReconciliation | null | undefined
}

const TONE_CLASS = {
  cleared: 'text-emerald-800',
  surfaced: 'text-amber-900',
  unchanged: 'text-slate-800',
} as const

export function CorrectionOutcome({
  status,
  fieldLabel,
  value,
  reconciliation,
}: CorrectionOutcomeProps) {
  // An error is already reported by the alert above the grid, and repeating it
  // here as an outcome would read as two separate failures.
  if (status === 'idle' || status === 'error' || fieldLabel === null) {
    return null
  }

  const message = status === 'success' ? reconciliationMessage(reconciliation) : null
  const headline =
    status === 'pending'
      ? 'Saving your correction'
      : message === null
        ? 'Correction saved'
        : reconciliationHeadline(message.tone)
  const tone = message === null ? 'text-slate-800' : TONE_CLASS[message.tone]

  return (
    <div
      role="status"
      aria-live="polite"
      className={`${CARD} border-l-4 border-l-sky-600 p-4`}
    >
      <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
        Last correction
      </h2>
      <p className={`mt-1 text-lg font-semibold ${tone}`}>{headline}</p>
      {status === 'pending' ? (
        <p className="mt-1 text-sm text-slate-700">
          Checking what it changed on this statement.
        </p>
      ) : (
        message !== null && <p className="mt-1 text-sm text-slate-700">{message.text}</p>
      )}
      <p className="mt-1 text-sm text-slate-500">
        {fieldLabel} saved as <span className="font-mono">{value ?? 'none'}</span>
      </p>
    </div>
  )
}
