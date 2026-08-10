import { describe, expect, it } from 'vitest'

import { reconciliationHeadline, reconciliationMessage } from '../reconciliation'

describe('what a correction changed, in words', () => {
  it('says one exception in the singular', () => {
    const message = reconciliationMessage({
      exceptions_removed: 1,
      exceptions_added: 0,
      dollars_at_risk_change_cents: -184_000,
    })
    expect(message?.text).toBe('Cleared 1 exception. $1,840.00 less at risk on this statement.')
  })

  it('says several exceptions in the plural', () => {
    const message = reconciliationMessage({
      exceptions_removed: 3,
      exceptions_added: 0,
      dollars_at_risk_change_cents: -184_000,
    })
    expect(message?.text).toBe('Cleared 3 exceptions. $1,840.00 less at risk on this statement.')
  })

  it('reads a fall in exposure as money the correction cleared', () => {
    const message = reconciliationMessage({
      exceptions_removed: 1,
      exceptions_added: 0,
      dollars_at_risk_change_cents: -184_000,
    })
    expect(message?.tone).toBe('cleared')
    expect(message?.text).toContain('$1,840.00 less at risk')
    // The sign belongs to the sentence, not to the figure printed in it.
    expect(message?.text).not.toContain('-$')
  })

  it('reads a rise in exposure as a real discrepancy the reviewer found', () => {
    const message = reconciliationMessage({
      exceptions_removed: 0,
      exceptions_added: 1,
      dollars_at_risk_change_cents: 184_000,
    })
    expect(message?.tone).toBe('surfaced')
    expect(reconciliationHeadline('surfaced')).toBe('Your correction found disputed money')
    expect(message?.text).toBe(
      'Opened 1 exception. $1,840.00 more at risk on this statement, ' +
        'money the earlier reading missed.',
    )
  })

  it('says plainly when a correction moved nothing', () => {
    const message = reconciliationMessage({
      exceptions_removed: 0,
      exceptions_added: 0,
      dollars_at_risk_change_cents: 0,
    })
    expect(message?.tone).toBe('unchanged')
    expect(message?.text).toBe(
      'No exceptions opened or closed, and no change to the money at risk.',
    )
  })

  it('has nothing to say when the document could not be reconciled', () => {
    expect(reconciliationMessage(null)).toBeNull()
  })

  it('treats a response with no reconciliation field as nothing to say', () => {
    // The API may be mid change and simply omit it. A missing field is not zero.
    expect(reconciliationMessage(undefined)).toBeNull()
  })

  it('prints both counts rather than netting them into one figure', () => {
    // Netting 2 closed against 1 opened into "net 1" would be a number the UI
    // invented, which is exactly what the standing rule forbids.
    const message = reconciliationMessage({
      exceptions_removed: 2,
      exceptions_added: 1,
      dollars_at_risk_change_cents: -50_000,
    })
    expect(message?.text).toBe(
      'Cleared 2 exceptions. Opened 1 exception. $500.00 less at risk on this statement.',
    )
  })

  it('leaves the money out when the exposure did not move', () => {
    // A timing difference closes an exception without moving a dollar, and
    // "$0.00 less at risk" reads as a bug rather than as a true statement.
    const message = reconciliationMessage({
      exceptions_removed: 1,
      exceptions_added: 0,
      dollars_at_risk_change_cents: 0,
    })
    expect(message?.text).toBe('Cleared 1 exception.')
    expect(message?.text).not.toContain('$0.00')
  })

  it('groups a large amount the way every other figure on screen is grouped', () => {
    const message = reconciliationMessage({
      exceptions_removed: 4,
      exceptions_added: 0,
      dollars_at_risk_change_cents: -1_234_567,
    })
    expect(message?.text).toContain('$12,345.67 less at risk')
  })
})
