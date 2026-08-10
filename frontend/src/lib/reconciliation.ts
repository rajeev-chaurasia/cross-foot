/**
 * Turning the reconciliation object a correction returns into a sentence.
 *
 * Every number in the sentence is one the API published, printed as it arrived.
 * The two exception counts are never netted against each other and the dollar
 * figure is never divided by anything: when a correction both closes and opens
 * exceptions, both counts get their own sentence, because a single "net 1" would
 * be a figure this code invented.
 *
 * Three cases are deliberately not zeros on a screen. A null reconciliation
 * means the document could not be reconciled, and the caller is expected to
 * print nothing at all rather than "0 exceptions, $0.00". A zero dollar change
 * beside a real exception change gets no money sentence, because a timing
 * difference moves an exception without moving a dollar. And a positive change
 * is a success: the reviewer's correction uncovered money that was being
 * disputed and nobody had noticed, so the wording says it was found rather than
 * implying the reviewer caused it.
 */

import type { CorrectionReconciliation } from '../api/types'
import { formatCentsAbsolute } from './format'

/** Which way the correction moved the money, for wording and for colour. */
export type ReconciliationTone = 'cleared' | 'surfaced' | 'unchanged'

export interface ReconciliationMessage {
  readonly tone: ReconciliationTone
  readonly text: string
}

const HEADLINES: Record<ReconciliationTone, string> = {
  cleared: 'Your correction cleared exceptions',
  surfaced: 'Your correction found disputed money',
  unchanged: 'Nothing on this statement changed',
}

/** "1 exception" or "4 exceptions", from the count the API sent. */
function exceptionCount(count: number): string {
  return `${count.toLocaleString('en-US')} exception${count === 1 ? '' : 's'}`
}

function toneOf(reconciliation: CorrectionReconciliation): ReconciliationTone {
  const dollars = reconciliation.dollars_at_risk_change_cents
  if (dollars < 0) {
    return 'cleared'
  }
  if (dollars > 0) {
    return 'surfaced'
  }
  if (reconciliation.exceptions_removed > 0) {
    return 'cleared'
  }
  if (reconciliation.exceptions_added > 0) {
    return 'surfaced'
  }
  return 'unchanged'
}

/** The headline that goes above the sentence, for the tone the sentence takes. */
export function reconciliationHeadline(tone: ReconciliationTone): string {
  return HEADLINES[tone]
}

/**
 * What a correction changed, in one or two plain sentences.
 *
 * Null in, null out: a document that could not be reconciled has nothing to
 * report and the caller shows no reconciliation line at all. An absent field
 * from an older API build reads the same way.
 */
export function reconciliationMessage(
  reconciliation: CorrectionReconciliation | null | undefined,
): ReconciliationMessage | null {
  if (reconciliation === null || reconciliation === undefined) {
    return null
  }

  const removed = reconciliation.exceptions_removed
  const added = reconciliation.exceptions_added
  const dollars = reconciliation.dollars_at_risk_change_cents

  const parts: string[] = []
  if (removed > 0) {
    parts.push(`Cleared ${exceptionCount(removed)}.`)
  }
  if (added > 0) {
    parts.push(`Opened ${exceptionCount(added)}.`)
  }
  if (dollars < 0) {
    parts.push(`${formatCentsAbsolute(dollars)} less at risk on this statement.`)
  } else if (dollars > 0) {
    parts.push(
      `${formatCentsAbsolute(dollars)} more at risk on this statement, ` +
        `money the earlier reading missed.`,
    )
  }
  if (parts.length === 0) {
    parts.push('No exceptions opened or closed, and no change to the money at risk.')
  }

  return { tone: toneOf(reconciliation), text: parts.join(' ') }
}
