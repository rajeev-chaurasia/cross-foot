import { describe, expect, it } from 'vitest'

import { METRICS } from '../../test/fixtures'
import type { ThresholdPoint } from '../../api/types'
import { binsFor, familiesOf, readSweep, sweepFor, THRESHOLD_SPLIT } from '../metrics'

describe('sweepFor', () => {
  it('keeps the published order, because the last entry is the held out result', () => {
    const points = sweepFor(METRICS.threshold_sweep, 'amount')
    expect(points.map((point) => point.threshold)).toEqual([0, 0.5, 0.7, 0.9, 0.7])
    const last = points[points.length - 1]
    expect(last.auto_accept_precision).toBe(0.9597)
    expect(last.review_rate).toBe(0.0534)
  })

  it('does not reorder a sweep whose final entry sorts before a curve point', () => {
    // Sorting by threshold would move the 0.7 result in front of the 0.9 curve
    // point, and nothing in a ThresholdPoint could tell them apart again.
    const thresholds = sweepFor(METRICS.threshold_sweep, 'amount').map((point) => point.threshold)
    expect(thresholds).not.toEqual([...thresholds].sort((left, right) => left - right))
  })

  it('takes only the named family', () => {
    expect(sweepFor(METRICS.threshold_sweep, 'reference')).toHaveLength(4)
    expect(sweepFor(METRICS.threshold_sweep, 'date')).toEqual([])
  })
})

describe('readSweep', () => {
  it('marks the applied point even when a curve point has higher precision', () => {
    const reading = readSweep(sweepFor(METRICS.threshold_sweep, 'amount'))
    expect(reading.kind).toBe('sweep')
    if (reading.kind !== 'sweep') return
    // The 0.9 curve point reaches 100 percent, which is the number the old rule
    // published. The applied point is the one the result was measured at.
    expect(reading.sweep.applied.threshold).toBe(0.7)
    expect(reading.sweep.applied.auto_accept_precision).toBe(0.9969)
    expect(
      reading.sweep.curve.some((point) => point.auto_accept_precision > 0.9969),
    ).toBe(true)
  })

  it('reads the held out result from the final entry, not from the curve', () => {
    const reading = readSweep(sweepFor(METRICS.threshold_sweep, 'reference'))
    expect(reading.kind).toBe('sweep')
    if (reading.kind !== 'sweep') return
    expect(reading.sweep.achieved.auto_accept_precision).toBe(0.9505)
    expect(reading.sweep.achieved.review_rate).toBe(0.8196)
    // The point chosen on calibration reads 100 percent at a lower review rate.
    expect(reading.sweep.applied.auto_accept_precision).toBe(1)
    expect(reading.sweep.applied.review_rate).toBe(0.7757)
    expect(reading.sweep.applied.threshold).toBe(reading.sweep.achieved.threshold)
  })

  it('leaves the achieved point out of the curve it is measured against', () => {
    const points = sweepFor(METRICS.threshold_sweep, 'amount')
    const reading = readSweep(points)
    if (reading.kind !== 'sweep') throw new Error('expected a readable sweep')
    expect(reading.sweep.curve).toHaveLength(points.length - 1)
    expect(reading.sweep.curve).not.toContain(reading.sweep.achieved)
  })

  it('calls a sweep of one point malformed rather than marking it', () => {
    const single: ThresholdPoint[] = [
      { field_family: 'amount', threshold: 0.9, auto_accept_precision: 1, review_rate: 0.78 },
    ]
    const reading = readSweep(single)
    expect(reading.kind).toBe('malformed')
    if (reading.kind !== 'malformed') return
    expect(reading.reason).toContain('1 sweep points')
  })

  it('calls a result at a threshold off the curve malformed', () => {
    const off: ThresholdPoint[] = [
      { field_family: 'amount', threshold: 0.5, auto_accept_precision: 0.98, review_rate: 0.02 },
      { field_family: 'amount', threshold: 0.7, auto_accept_precision: 0.96, review_rate: 0.05 },
    ]
    const reading = readSweep(off)
    expect(reading.kind).toBe('malformed')
    if (reading.kind !== 'malformed') return
    expect(reading.reason).toContain('not a point on its published curve')
  })
})

describe('grouping', () => {
  it('lists families in the contract order, skipping the absent ones', () => {
    expect(familiesOf(METRICS.threshold_sweep)).toEqual(['amount', 'reference'])
  })

  it('sorts calibration bins by mean confidence', () => {
    expect(binsFor(METRICS.calibration, 'amount')).toHaveLength(1)
    expect(binsFor(METRICS.calibration, 'text')).toEqual([])
  })

  it('names the only split a threshold may be chosen on', () => {
    expect(THRESHOLD_SPLIT).toBe('calibration')
    expect(THRESHOLD_SPLIT).not.toBe(METRICS.scorecard.split)
  })
})
