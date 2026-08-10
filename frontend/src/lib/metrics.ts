/**
 * Helpers for reading a scorecard, none of which invent a number.
 *
 * A published threshold sweep says two different things in one flat array, and
 * which is which is carried by POSITION rather than by a field. The layout is
 * `crossfoot.evals.plots.family_sweeps`, and this module is the second reader of
 * it, so it reads it the same way rather than inventing a second interpretation:
 *
 *   per family, the curve measured on the CALIBRATION split in ascending
 *   threshold order, then ONE FINAL ENTRY holding what the scorecard's own
 *   reported split reached at the threshold that was applied.
 *
 * Two consequences follow and both used to be got wrong here. Sorting the array
 * destroys the layout beyond recovery, so nothing below sorts it. And picking
 * the entry with the best auto accept precision picks a calibration point every
 * time, because the calibration split is the one the threshold was fitted on;
 * the operating point is the one whose threshold the final entry names, not the
 * one that flatters the product. A run that does not obey the layout is reported
 * as malformed rather than guessed at, exactly as the Python reader raises.
 */

import type { CalibrationBin, FieldFamily, SplitName, ThresholdPoint } from '../api/types'
import { formatThreshold } from './format'

export const FAMILY_COLOR: Record<FieldFamily, string> = {
  amount: '#0284c7',
  date: '#059669',
  reference: '#7c3aed',
  text: '#d97706',
}

/**
 * The split a threshold may be chosen on, which is never the reported one.
 * Fixed by `crossfoot.confidence.calibration.THRESHOLD_SPLIT`; the scorecard has
 * no field for it, so naming it here is what lets every number on screen say
 * which split it came from.
 */
export const THRESHOLD_SPLIT: SplitName = 'calibration'

/** One family's sweep, split into the curve, the choice, and the result. */
export interface FamilySweep {
  readonly field_family: FieldFamily
  /** Measured on the calibration split, in ascending threshold order. */
  readonly curve: readonly ThresholdPoint[]
  /** The curve point a threshold was chosen at: a calibration figure. */
  readonly applied: ThresholdPoint
  /** What the reported split reached at that same threshold: the held out figure. */
  readonly achieved: ThresholdPoint
}

/** A sweep read, or the reason it could not be read. Never a guess. */
export type SweepReading =
  | { readonly kind: 'sweep'; readonly sweep: FamilySweep }
  | { readonly kind: 'malformed'; readonly reason: string }

/** The families present in a set of rows, in the contract's family order. */
export function familiesOf(rows: readonly { field_family: FieldFamily }[]): FieldFamily[] {
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

/**
 * One family's published points, in the order the scorecard published them.
 *
 * Filtering preserves relative order, which is the whole contract. This function
 * deliberately does not sort: the final entry is the held out result and sorting
 * by threshold buries it somewhere in the middle of the calibration curve, where
 * nothing can tell it apart again.
 */
export function sweepFor(
  points: readonly ThresholdPoint[],
  family: FieldFamily,
): ThresholdPoint[] {
  return points.filter((point) => point.field_family === family)
}

/**
 * Recover a family's curve, its operating point, and what the reported split
 * delivered there, from the positional layout described at the top of this file.
 */
export function readSweep(points: readonly ThresholdPoint[]): SweepReading {
  if (points.length < 2) {
    return {
      kind: 'malformed',
      reason: `this family publishes ${points.length} sweep points, which is not a curve and a result`,
    }
  }
  const curve = points.slice(0, -1)
  const achieved = points[points.length - 1]
  const applied = curve.find((point) => point.threshold === achieved.threshold)
  if (applied === undefined) {
    return {
      kind: 'malformed',
      reason: `this family reports a result at threshold ${formatThreshold(achieved.threshold)}, which is not a point on its published curve`,
    }
  }
  return {
    kind: 'sweep',
    sweep: { field_family: achieved.field_family, curve, applied, achieved },
  }
}
