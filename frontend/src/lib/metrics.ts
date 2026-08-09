/**
 * Helpers for reading a scorecard, none of which invent a number.
 *
 * The contract asks for "the threshold sweep with the chosen operating point
 * marked" but the sweep carries no field saying which point was chosen, so the
 * choice has to be a rule rather than a lookup. The rule is the one the product
 * claim rests on: the highest published auto accept precision, and among ties
 * the smallest published review rate. The rule is printed next to the marker on
 * screen so nobody has to guess what the dot means.
 */

import type { CalibrationBin, FieldFamily, ThresholdPoint } from '../api/types'

export const FAMILY_COLOR: Record<FieldFamily, string> = {
  amount: '#0284c7',
  date: '#059669',
  reference: '#7c3aed',
  text: '#d97706',
}

export function operatingPoint(points: readonly ThresholdPoint[]): ThresholdPoint | undefined {
  let best: ThresholdPoint | undefined
  for (const point of points) {
    if (best === undefined) {
      best = point
      continue
    }
    if (point.auto_accept_precision > best.auto_accept_precision) {
      best = point
    } else if (
      point.auto_accept_precision === best.auto_accept_precision &&
      point.review_rate < best.review_rate
    ) {
      best = point
    }
  }
  return best
}

/** The families present in a set of rows, in the contract's family order. */
export function familiesOf(
  rows: readonly { field_family: FieldFamily }[],
): FieldFamily[] {
  const order: FieldFamily[] = ['amount', 'date', 'reference', 'text']
  return order.filter((family) => rows.some((row) => row.field_family === family))
}

export function binsFor(
  bins: readonly CalibrationBin[],
  family: FieldFamily,
): CalibrationBin[] {
  return bins
    .filter((bin) => bin.field_family === family)
    .slice()
    .sort((left, right) => left.mean_confidence - right.mean_confidence)
}

export function sweepFor(
  points: readonly ThresholdPoint[],
  family: FieldFamily,
): ThresholdPoint[] {
  return points
    .filter((point) => point.field_family === family)
    .slice()
    .sort((left, right) => left.threshold - right.threshold)
}
