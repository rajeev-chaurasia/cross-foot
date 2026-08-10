/**
 * Reliability diagram drawn straight from the calibration bins.
 *
 * Plain SVG rather than a charting dependency: two axes, an ideal diagonal and
 * one dot per published bin is the whole picture. Every dot is a pair the API
 * sent, and the same pairs are repeated in a table for screen readers.
 */

import type { CalibrationBin } from '../../api/types'
import { binsFor, familiesOf, FAMILY_COLOR } from '../../lib/metrics'
import { formatRate, humanize } from '../../lib/format'

const WIDTH = 360
const HEIGHT = 320
const PAD_LEFT = 46
const PAD_BOTTOM = 40
const PAD_TOP = 12
const PAD_RIGHT = 12

const TICKS = [0, 0.25, 0.5, 0.75, 1]

function x(value: number): number {
  return PAD_LEFT + value * (WIDTH - PAD_LEFT - PAD_RIGHT)
}

function y(value: number): number {
  return HEIGHT - PAD_BOTTOM - value * (HEIGHT - PAD_TOP - PAD_BOTTOM)
}

interface Props {
  bins: CalibrationBin[]
  runId: string
}

export function ReliabilityDiagram({ bins, runId }: Props) {
  const families = familiesOf(bins)

  return (
    <figure className="m-0 grid gap-4 lg:grid-cols-[minmax(0,24rem)_minmax(0,1fr)] lg:items-start">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full max-w-md"
        role="img"
        aria-label="Reliability diagram: mean confidence against empirical accuracy, with the ideal diagonal"
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
            <line x1={x(tick)} y1={PAD_TOP} x2={x(tick)} y2={y(0)} stroke="#f1f5f9" />
            <line x1={PAD_LEFT} y1={y(tick)} x2={x(1)} y2={y(tick)} stroke="#f1f5f9" />
            <text x={x(tick)} y={HEIGHT - PAD_BOTTOM + 14} textAnchor="middle" fontSize="10" fill="#64748b">
              {tick}
            </text>
            <text x={PAD_LEFT - 6} y={y(tick) + 3} textAnchor="end" fontSize="10" fill="#64748b">
              {tick}
            </text>
          </g>
        ))}
        <line
          x1={x(0)}
          y1={y(0)}
          x2={x(1)}
          y2={y(1)}
          stroke="#94a3b8"
          strokeDasharray="4 3"
          data-testid="ideal-diagonal"
        />
        {families.map((family) => {
          const familyBins = binsFor(bins, family)
          const path = familyBins
            .map((bin) => `${x(bin.mean_confidence)},${y(bin.empirical_accuracy)}`)
            .join(' ')
          return (
            <g key={family}>
              {familyBins.length > 1 && (
                <polyline points={path} fill="none" stroke={FAMILY_COLOR[family]} strokeWidth="1.5" />
              )}
              {familyBins.map((bin) => (
                <circle
                  key={`${bin.mean_confidence}-${bin.count}`}
                  cx={x(bin.mean_confidence)}
                  cy={y(bin.empirical_accuracy)}
                  r="4"
                  fill={FAMILY_COLOR[family]}
                  data-testid="calibration-point"
                />
              ))}
            </g>
          )
        })}
        <text x={x(0.5)} y={HEIGHT - 6} textAnchor="middle" fontSize="11" fill="#334155">
          Mean confidence
        </text>
        <text
          x={12}
          y={y(0.5)}
          textAnchor="middle"
          fontSize="11"
          fill="#334155"
          transform={`rotate(-90 12 ${y(0.5)})`}
        >
          Empirical accuracy
        </text>
      </svg>

      {/* The same pairs the dots are drawn from, printed rather than hidden: it
          fills the space beside a square chart with the numbers themselves, and
          a screen reader and a sighted reader then hear the same table. */}
      {/* Capped to about the height of the chart beside it: a scorecard can
          publish forty bins, and an uncapped table turns a square figure into a
          thousand pixels of white space on its left. */}
      <div className="min-w-0 max-h-[21rem] overflow-y-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Calibration bins</caption>
          <thead className="sticky top-0 bg-white text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium">
                Family
              </th>
              <th scope="col" className="py-1 pr-3 text-right font-medium">
                Mean confidence
              </th>
              <th scope="col" className="py-1 pr-3 text-right font-medium">
                Empirical accuracy
              </th>
              <th scope="col" className="py-1 text-right font-medium">
                Fields
              </th>
            </tr>
          </thead>
          <tbody>
            {bins.map((bin) => (
              <tr key={`${bin.field_family}-${bin.mean_confidence}`} className="border-t border-slate-100">
                <th scope="row" className="py-1.5 pr-3 font-normal text-slate-700">
                  <span
                    aria-hidden="true"
                    className="mr-1.5 inline-block h-2.5 w-2.5 rounded-full align-middle"
                    style={{ backgroundColor: FAMILY_COLOR[bin.field_family] }}
                  />
                  {humanize(bin.field_family)}
                </th>
                <td className="py-1.5 pr-3 text-right font-mono text-slate-900">
                  {formatRate(bin.mean_confidence)}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono text-slate-900">
                  {formatRate(bin.empirical_accuracy)}
                </td>
                <td className="py-1.5 text-right font-mono text-slate-600">{bin.count}</td>
              </tr>
            ))}
          </tbody>
        </table>

      </div>

      <figcaption className="text-sm text-slate-600 lg:col-span-2">
        Reliability diagram, run <span className="font-mono">{runId}</span>. A dot on the dashed
        line means the confidence was telling the truth.
        <span className="mt-2 flex flex-wrap gap-3 text-xs text-slate-600">
          {families.map((family) => (
            <span key={family} className="flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: FAMILY_COLOR[family] }}
              />
              {humanize(family)}
            </span>
          ))}
        </span>
      </figcaption>
    </figure>
  )
}
