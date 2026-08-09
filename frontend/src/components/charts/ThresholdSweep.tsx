/**
 * Threshold sweep for one field family, with the operating point marked.
 *
 * Two published series against the auto accept threshold: the precision of what
 * gets auto accepted, and the share of fields that go to a human instead. The
 * marked point is the one lib/metrics picks, and the caption says which rule
 * picked it because the sweep does not carry that flag.
 */

import type { FieldFamily, ThresholdPoint } from '../../api/types'
import { operatingPoint, FAMILY_COLOR } from '../../lib/metrics'
import { formatRate, humanize } from '../../lib/format'

const WIDTH = 360
const HEIGHT = 260
const PAD_LEFT = 46
const PAD_BOTTOM = 40
const PAD_TOP = 12
const PAD_RIGHT = 12

const TICKS = [0, 0.25, 0.5, 0.75, 1]

const REVIEW_COLOR = '#475569'

function y(value: number): number {
  return HEIGHT - PAD_BOTTOM - value * (HEIGHT - PAD_TOP - PAD_BOTTOM)
}

interface Props {
  family: FieldFamily
  points: ThresholdPoint[]
}

export function ThresholdSweep({ family, points }: Props) {
  const thresholds = points.map((point) => point.threshold)
  const low = Math.min(...thresholds)
  const high = Math.max(...thresholds)
  const span = high - low
  const x = (value: number): number => {
    const fraction = span === 0 ? 0.5 : (value - low) / span
    return PAD_LEFT + fraction * (WIDTH - PAD_LEFT - PAD_RIGHT)
  }
  const marked = operatingPoint(points)

  const line = (pick: (point: ThresholdPoint) => number): string =>
    points.map((point) => `${x(point.threshold)},${y(pick(point))}`).join(' ')

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full max-w-md"
        role="img"
        aria-label={`Threshold sweep for ${humanize(family)} fields: auto accept precision and review rate against the confidence threshold`}
      >
        <rect
          x={PAD_LEFT}
          y={PAD_TOP}
          width={WIDTH - PAD_LEFT - PAD_RIGHT}
          height={HEIGHT - PAD_TOP - PAD_BOTTOM}
          fill="#ffffff"
          stroke="#e2e8f0"
        />
        {TICKS.map((tick) => (
          <g key={tick}>
            <line x1={PAD_LEFT} y1={y(tick)} x2={WIDTH - PAD_RIGHT} y2={y(tick)} stroke="#f1f5f9" />
            <text x={PAD_LEFT - 6} y={y(tick) + 3} textAnchor="end" fontSize="10" fill="#64748b">
              {tick}
            </text>
          </g>
        ))}
        {points.map((point) => (
          <text
            key={point.threshold}
            x={x(point.threshold)}
            y={HEIGHT - PAD_BOTTOM + 14}
            textAnchor="middle"
            fontSize="10"
            fill="#64748b"
          >
            {point.threshold}
          </text>
        ))}

        {points.length > 1 && (
          <>
            <polyline
              points={line((point) => point.auto_accept_precision)}
              fill="none"
              stroke={FAMILY_COLOR[family]}
              strokeWidth="1.5"
            />
            <polyline
              points={line((point) => point.review_rate)}
              fill="none"
              stroke={REVIEW_COLOR}
              strokeWidth="1.5"
              strokeDasharray="4 3"
            />
          </>
        )}
        {points.map((point) => (
          <g key={`dots-${point.threshold}`}>
            <circle
              cx={x(point.threshold)}
              cy={y(point.auto_accept_precision)}
              r="3"
              fill={FAMILY_COLOR[family]}
            />
            <circle
              cx={x(point.threshold)}
              cy={y(point.review_rate)}
              r="3"
              fill={REVIEW_COLOR}
            />
          </g>
        ))}
        {marked !== undefined && (
          <g data-testid="operating-point">
            <line
              x1={x(marked.threshold)}
              y1={PAD_TOP}
              x2={x(marked.threshold)}
              y2={y(0)}
              stroke="#b91c1c"
              strokeWidth="1"
            />
            <circle
              cx={x(marked.threshold)}
              cy={y(marked.auto_accept_precision)}
              r="6"
              fill="none"
              stroke="#b91c1c"
              strokeWidth="2"
            />
          </g>
        )}
        <text x={(PAD_LEFT + WIDTH - PAD_RIGHT) / 2} y={HEIGHT - 6} textAnchor="middle" fontSize="11" fill="#334155">
          Auto accept threshold
        </text>
      </svg>

      <figcaption className="mt-2 text-sm text-slate-600">
        {humanize(family)} fields.{' '}
        {marked === undefined ? (
          'No sweep points were published for this family.'
        ) : (
          <>
            Operating point at threshold{' '}
            <span className="font-mono">{marked.threshold}</span>:{' '}
            <span className="font-mono">{formatRate(marked.auto_accept_precision, 2)}</span> auto
            accept precision while sending{' '}
            <span className="font-mono">{formatRate(marked.review_rate)}</span> to review. Marked as
            the published point with the highest auto accept precision.
          </>
        )}
      </figcaption>

      <ul className="mt-2 flex flex-wrap gap-3 text-xs text-slate-600">
        <li className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: FAMILY_COLOR[family] }}
          />
          Auto accept precision
        </li>
        <li className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: REVIEW_COLOR }}
          />
          Review rate
        </li>
      </ul>

      <table className="sr-only">
        <caption>Threshold sweep for {humanize(family)} fields</caption>
        <thead>
          <tr>
            <th scope="col">Threshold</th>
            <th scope="col">Auto accept precision</th>
            <th scope="col">Review rate</th>
          </tr>
        </thead>
        <tbody>
          {points.map((point) => (
            <tr key={point.threshold}>
              <td>{point.threshold}</td>
              <td>{formatRate(point.auto_accept_precision, 2)}</td>
              <td>{formatRate(point.review_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
