import { describe, expect, it } from 'vitest'

import { METRICS } from '../../test/fixtures'
import { binsFor, familiesOf, operatingPoint, sweepFor } from '../metrics'

describe('operatingPoint', () => {
  it('marks the published point with the highest auto accept precision', () => {
    const point = operatingPoint(sweepFor(METRICS.threshold_sweep, 'amount'))
    expect(point?.threshold).toBe(0.9)
    expect(point?.review_rate).toBe(0.181)
  })

  it('breaks a precision tie on the smaller review rate', () => {
    const point = operatingPoint([
      { field_family: 'amount', threshold: 0.6, auto_accept_precision: 0.99, review_rate: 0.4 },
      { field_family: 'amount', threshold: 0.8, auto_accept_precision: 0.99, review_rate: 0.2 },
    ])
    expect(point?.threshold).toBe(0.8)
  })

  it('has nothing to mark on an empty sweep', () => {
    expect(operatingPoint([])).toBeUndefined()
  })
})

describe('grouping', () => {
  it('lists families in the contract order, skipping the absent ones', () => {
    expect(familiesOf(METRICS.threshold_sweep)).toEqual(['amount', 'reference'])
  })

  it('sorts sweep points by threshold', () => {
    expect(sweepFor(METRICS.threshold_sweep, 'amount').map((point) => point.threshold)).toEqual([
      0.5, 0.9,
    ])
  })

  it('sorts calibration bins by mean confidence', () => {
    expect(binsFor(METRICS.calibration, 'amount')).toHaveLength(1)
    expect(binsFor(METRICS.calibration, 'text')).toEqual([])
  })
})
