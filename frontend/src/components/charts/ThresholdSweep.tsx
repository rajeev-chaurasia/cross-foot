/**
 * Threshold sweep for one field family, drawn the way the published PNG draws it.
 *
 * Auto accept precision against review rate. The curve is the sweep on the
 * calibration split, the only split a threshold may be chosen on. The filled
 * marker is the point chosen there; the hollow marker is what the held out split
 * reached at that same threshold, and the arrow between them is the
 * generalization gap. Nothing on this figure is unlabelled: every number printed
 * beside it names the split it was measured on, because a calibration precision
 * read as a test result is the exact mistake this chart exists to prevent.
 *
 * Which point is which comes from lib/metrics, which reads the scorecard's
 * positional layout. A malformed sweep is said to be malformed rather than
 * drawn.
 */

import type { FieldFamily, SplitName, ThresholdPoint } from '../../api/types'
import { FAMILY_COLOR, readSweep, THRESHOLD_SPLIT } from '../../lib/metrics'
import { formatRate, humanize } from '../../lib/format'

const WIDTH = 380
const HEIGHT = 280
const PAD_LEFT = 54
const PAD_BOTTOM = 46
const PAD_TOP = 14
const PAD_RIGHT = 16

// Headroom around the drawn points, in the fractions the scorecard publishes.
const Y_PAD = 0.02
const X_PAD = 0.04
const TICKS = 4

const ACHIEVED_COLOR = '#0f172a'
const GAP_COLOR = '#475569'

// Reported figures carry two decimals so a reader can check them straight
// against the scorecard JSON. Axis ticks are a scale, not a claim, so they round.
const REPORTED_DIGITS = 2
const TICK_DIGITS = 1

interface Props {
  family: FieldFamily
  /** The family's points, in the scorecard's published order. Never sorted. */
  points: ThresholdPoint[]
  /** The split the scorecard reports, which the final point was measured on. */
  reportedSplit: SplitName
}

function ticksBetween(low: number, high: number): number[] {
  return Array.from({ length: TICKS + 1 }, (_, index) => low + ((high - low) * index) / TICKS)
}

export function ThresholdSweep({ family, points, reportedSplit }: Props) {
  const reading = readSweep(points)

  if (reading.kind === 'malformed') {
    return (
      <figure className="m-0">
        <figcaption className="text-sm text-slate-600">
          {humanize(family)} fields. No operating point can be marked: {reading.reason}. The sweep is
          reported as unreadable rather than drawn from a guess.
        </figcaption>
      </figure>
    )
  }

  const { curve, applied, achieved } = reading.sweep
  // Ascending review rate, so the polyline traces the curve rather than the
  // order the thresholds happen to sit in. Ordering a copy leaves the published
  // array, whose order identifies the held out point, untouched.
  const drawn = curve.slice().sort((left, right) => left.review_rate - right.review_rate)
  const reviews = [...drawn.map((point) => point.review_rate), achieved.review_rate]
  const precisions = [...drawn.map((point) => point.auto_accept_precision), achieved.auto_accept_precision]

  const xLow = Math.max(0, Math.min(...reviews) - X_PAD)
  const xHigh = Math.min(1, Math.max(...reviews) + X_PAD)
  const yLow = Math.max(0, Math.min(...precisions) - Y_PAD)
  const yHigh = 1

  const x = (value: number): number =>
    PAD_LEFT + ((value - xLow) / (xHigh - xLow || 1)) * (WIDTH - PAD_LEFT - PAD_RIGHT)
  const y = (value: number): number =>
    HEIGHT - PAD_BOTTOM - ((value - yLow) / (yHigh - yLow || 1)) * (HEIGHT - PAD_TOP - PAD_BOTTOM)

  const arrowId = `sweep-gap-${family}`
  const appliedAt: [number, number] = [x(applied.review_rate), y(applied.auto_accept_precision)]
  const achievedAt: [number, number] = [x(achieved.review_rate), y(achieved.auto_accept_precision)]
  const reported = humanize(reportedSplit)

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full max-w-md"
        role="img"
        aria-label={`Threshold sweep for ${humanize(family)} fields: auto accept precision against review rate on the ${THRESHOLD_SPLIT} split, with the operating point and what the ${reportedSplit} split reached at the same threshold`}
      >
        <defs>
          <marker
            id={arrowId}
            markerWidth="6"
            markerHeight="6"
            refX="5"
            refY="3"
            orient="auto"
            markerUnits="strokeWidth"
          >
            <path d="M0,0 L6,3 L0,6 z" fill={GAP_COLOR} />
          </marker>
        </defs>

        <rect
          x={PAD_LEFT}
          y={PAD_TOP}
          width={WIDTH - PAD_LEFT - PAD_RIGHT}
          height={HEIGHT - PAD_TOP - PAD_BOTTOM}
          fill="#ffffff"
          stroke="#e2e8f0"
        />
        {ticksBetween(yLow, yHigh).map((tick) => (
          <g key={`y-${tick}`}>
            <line x1={PAD_LEFT} y1={y(tick)} x2={WIDTH - PAD_RIGHT} y2={y(tick)} stroke="#f1f5f9" />
            <text x={PAD_LEFT - 6} y={y(tick) + 3} textAnchor="end" fontSize="10" fill="#64748b">
              {formatRate(tick, TICK_DIGITS)}
            </text>
          </g>
        ))}
        {ticksBetween(xLow, xHigh).map((tick) => (
          <text
            key={`x-${tick}`}
            x={x(tick)}
            y={HEIGHT - PAD_BOTTOM + 14}
            textAnchor="middle"
            fontSize="10"
            fill="#64748b"
          >
            {formatRate(tick, TICK_DIGITS)}
          </text>
        ))}

        <polyline
          data-testid="calibration-curve"
          points={drawn.map((point) => `${x(point.review_rate)},${y(point.auto_accept_precision)}`).join(' ')}
          fill="none"
          stroke={FAMILY_COLOR[family]}
          strokeWidth="1.5"
        />

        <line
          data-testid="generalization-gap"
          x1={appliedAt[0]}
          y1={appliedAt[1]}
          x2={achievedAt[0]}
          y2={achievedAt[1]}
          stroke={GAP_COLOR}
          strokeWidth="1.2"
          markerEnd={`url(#${arrowId})`}
        />

        <circle
          data-testid="operating-point"
          cx={appliedAt[0]}
          cy={appliedAt[1]}
          r="5"
          fill={FAMILY_COLOR[family]}
          stroke="#ffffff"
          strokeWidth="1.5"
        >
          <title>
            {`Chosen on the ${THRESHOLD_SPLIT} split: ${formatRate(applied.auto_accept_precision, REPORTED_DIGITS)} precision at ${formatRate(applied.review_rate, REPORTED_DIGITS)} review`}
          </title>
        </circle>
        <circle
          data-testid="achieved-point"
          cx={achievedAt[0]}
          cy={achievedAt[1]}
          r="5"
          fill="#ffffff"
          stroke={ACHIEVED_COLOR}
          strokeWidth="2"
        >
          <title>
            {`Delivered on the ${reportedSplit} split: ${formatRate(achieved.auto_accept_precision, REPORTED_DIGITS)} precision at ${formatRate(achieved.review_rate, REPORTED_DIGITS)} review`}
          </title>
        </circle>

        <text
          x={(PAD_LEFT + WIDTH - PAD_RIGHT) / 2}
          y={HEIGHT - 6}
          textAnchor="middle"
          fontSize="11"
          fill="#334155"
        >
          Review rate, share of fields sent to a human
        </text>
      </svg>

      <figcaption className="mt-2 text-sm text-slate-600">
        {humanize(family)} fields, threshold{' '}
        <span className="font-mono">{applied.threshold.toFixed(4)}</span>. Chosen on the{' '}
        {THRESHOLD_SPLIT} split, where it read{' '}
        <span className="font-mono">{formatRate(applied.auto_accept_precision, REPORTED_DIGITS)}</span> auto accept
        precision at <span className="font-mono">{formatRate(applied.review_rate, REPORTED_DIGITS)}</span> review. On
        the held out {reportedSplit} split the same threshold delivered{' '}
        <span className="font-mono">{formatRate(achieved.auto_accept_precision, REPORTED_DIGITS)}</span> auto
        accept precision at <span className="font-mono">{formatRate(achieved.review_rate, REPORTED_DIGITS)}</span>{' '}
        review. The arrow is the distance between the two.
      </figcaption>

      <ul className="mt-2 flex flex-wrap gap-3 text-xs text-slate-600">
        <li className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: FAMILY_COLOR[family] }}
          />
          {humanize(THRESHOLD_SPLIT)} split sweep, and the point chosen on it
        </li>
        <li className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-2.5 w-2.5 rounded-full border-2 border-slate-900 bg-white"
          />
          {reported} split, at that same threshold
        </li>
      </ul>

      <table className="sr-only">
        <caption>Threshold sweep for {humanize(family)} fields</caption>
        <thead>
          <tr>
            <th scope="col">Split</th>
            <th scope="col">Threshold</th>
            <th scope="col">Auto accept precision</th>
            <th scope="col">Review rate</th>
          </tr>
        </thead>
        <tbody>
          {drawn.map((point) => (
            <tr key={`${THRESHOLD_SPLIT}-${point.threshold}`}>
              <td>{humanize(THRESHOLD_SPLIT)}</td>
              <td>{point.threshold}</td>
              <td>{formatRate(point.auto_accept_precision, REPORTED_DIGITS)}</td>
              <td>{formatRate(point.review_rate, REPORTED_DIGITS)}</td>
            </tr>
          ))}
          <tr>
            <td>{reported}</td>
            <td>{achieved.threshold}</td>
            <td>{formatRate(achieved.auto_accept_precision, REPORTED_DIGITS)}</td>
            <td>{formatRate(achieved.review_rate, REPORTED_DIGITS)}</td>
          </tr>
        </tbody>
      </table>
    </figure>
  )
}
