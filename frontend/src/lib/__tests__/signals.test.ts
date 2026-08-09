import { describe, expect, it } from 'vitest'

import { AMOUNT_DETAIL, CLAIM_DETAIL, VIN_DETAIL } from '../../test/fixtures'
import { flagReasons, signalRows } from '../signals'

describe('signalRows', () => {
  it('names a failed VIN check digit rather than showing a bare score', () => {
    const rows = signalRows(VIN_DETAIL.signals, 'reference', 'vin')
    const validator = rows.find((row) => row.key === 'validator_pass')
    expect(validator?.verdict).toBe('fail')
    expect(validator?.note).toContain('VIN check digit failed')
  })

  it('marks a missing signal as unavailable rather than as a failure', () => {
    const rows = signalRows(VIN_DETAIL.signals, 'reference', 'vin')
    const crossfoot = rows.find((row) => row.key === 'crossfoot_ok')
    expect(crossfoot?.verdict).toBe('unavailable')
    expect(crossfoot?.display).toBe('not available')
  })

  it('reports the crossfoot residual pointing at the line', () => {
    const rows = signalRows(AMOUNT_DETAIL.signals, 'amount', 'line_amount')
    const residual = rows.find((row) => row.key === 'crossfoot_residual_suspect')
    expect(residual?.verdict).toBe('fail')
    expect(residual?.display).toBe('suspect')
  })

  it('carries the route as context, not as a verdict', () => {
    const rows = signalRows(CLAIM_DETAIL.signals, 'reference', 'claim_number')
    const route = rows.find((row) => row.key === 'route')
    expect(route?.verdict).toBe('info')
    expect(route?.display).toBe('Scanned pdf')
  })
})

describe('flagReasons', () => {
  it('returns only the failing signals, which is why the field was flagged', () => {
    const reasons = flagReasons(signalRows(VIN_DETAIL.signals, 'reference', 'vin'))
    expect(reasons).toHaveLength(4)
    expect(reasons.join(' ')).toContain('VIN check digit failed')
    expect(reasons.join(' ')).toContain('0 against O')
  })

  it('says nothing when every signal passed', () => {
    const reasons = flagReasons(
      signalRows(
        {
          self_consistency: 1,
          det_llm_agreement: 1,
          validator_pass: 1,
          grammar_match: 1,
          crossfoot_ok: 1,
          crossfoot_residual_suspect: false,
          char_ambiguity: 0,
          route: 'csv',
        },
        'amount',
        'line_amount',
      ),
    )
    expect(reasons).toEqual([])
  })
})
