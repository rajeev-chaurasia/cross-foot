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
 *
 * The live region around it is mounted from the first render and never taken
 * out again. A `role="status"` element inserted into the page at the moment it
 * has something to say is missed by some screen readers, so the container is
 * always there and only its contents change.
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
  /** What the reviewer typed, which is all there is while the write is open. */
  typed: string | null
  /** The value the API stored, which is what it really saved. */
  saved: string | null
  reconciliation: CorrectionReconciliation | null | undefined
}

const TONE_CLASS = {
  cleared: 'text-emerald-800',
  surfaced: 'text-amber-900',
  unchanged: 'text-slate-800',
} as const

const PANEL = `${CARD} border-l-4 border-l-sky-600 p-4`

export function CorrectionOutcome({
  status,
  fieldLabel,
  typed,
  saved,
  reconciliation,
}: CorrectionOutcomeProps) {
  // An error is already reported by the alert above the grid, and repeating it
  // here as an outcome would read as two separate failures.
  const claimed = (status === 'pending' || status === 'success') && fieldLabel !== null

  const message = status === 'success' ? reconciliationMessage(reconciliation) : null
  const headline =
    status === 'pending'
      ? 'Saving your correction'
      : message === null
        ? 'Correction saved'
        : reconciliationHeadline(message.tone)
  const tone = message === null ? 'text-slate-800' : TONE_CLASS[message.tone]
  // The typed text stands in only until the answer lands. A reviewer who types
  // $1,840 has 1840.00 stored and 7/4/2026 is stored as 2026-07-04, so echoing
  // the draft made the panel report a value the database does not hold.
  const value = status === 'pending' ? typed : saved

  return (
    <div role="status" aria-live="polite" className={claimed ? PANEL : 'sr-only'}>
      {claimed && (
        <>
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
            {fieldLabel} {status === 'pending' ? 'saving as' : 'saved as'}{' '}
            <span className="break-all font-mono">{value ?? 'none'}</span>
          </p>
        </>
      )}
    </div>
  )
}
