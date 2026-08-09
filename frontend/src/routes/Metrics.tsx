/**
 * The published numbers, and nothing else.
 *
 * Every figure on this page is read from GET /api/metrics or
 * GET /api/stats/summary. The accuracy grid prints the counts the scorecard
 * published rather than a rate derived from them, because the scorecard does
 * not publish a per cell rate and this screen is not allowed to invent one.
 */

import { useMetrics, useSummary } from '../api/queries'
import type { FieldAccuracyCell, QualityTier } from '../api/types'
import { QUALITY_TIERS } from '../api/types'
import { ReliabilityDiagram } from '../components/charts/ReliabilityDiagram'
import { ThresholdSweep } from '../components/charts/ThresholdSweep'
import { CARD } from '../components/ui'
import { formatMicroUsd, formatRate, formatTimestamp, humanize } from '../lib/format'
import { familiesOf, sweepFor } from '../lib/metrics'

interface TileProps {
  label: string
  value: string
  note?: string
}

function Tile({ label, value, note }: TileProps) {
  return (
    <div className={`${CARD} p-4`}>
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-slate-900">{value}</p>
      {note !== undefined && <p className="mt-1 text-xs text-slate-500">{note}</p>}
    </div>
  )
}

function cellFor(
  cells: FieldAccuracyCell[],
  family: string,
  tier: QualityTier,
): FieldAccuracyCell | undefined {
  return cells.find((cell) => cell.field_family === family && cell.quality_tier === tier)
}

export function Metrics() {
  const metrics = useMetrics()
  const summary = useSummary()

  if (metrics.isError) {
    return (
      <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">
        The metrics could not be loaded. {metrics.error.message}
      </p>
    )
  }

  if (metrics.data === undefined) {
    return <p className="text-sm text-slate-600">Loading the published metrics.</p>
  }

  const { scorecard, calibration, threshold_sweep: sweep } = metrics.data
  const accuracyFamilies = familiesOf(scorecard.field_accuracy)
  const tiers = QUALITY_TIERS.filter((tier) =>
    scorecard.field_accuracy.some((cell) => cell.quality_tier === tier),
  )
  const sweepFamilies = familiesOf(sweep)

  return (
    <div className="space-y-4">
      <section className={`${CARD} p-4`} aria-labelledby="metrics-heading">
        <h1 id="metrics-heading" className="text-sm font-medium uppercase tracking-wide text-slate-500">
          Published metrics
        </h1>
        <p className="mt-1 text-2xl font-semibold text-slate-900">
          Scorecard <span className="font-mono">{scorecard.run_id}</span>
        </p>
        <p className="mt-1 text-sm text-slate-600">
          {formatTimestamp(scorecard.created_at)}, commit{' '}
          <span className="font-mono">{scorecard.git_sha}</span>, {humanize(scorecard.split)} split,
          seed {scorecard.master_seed}.{' '}
          {scorecard.models_used.length === 0
            ? 'No model was called on this run.'
            : `Models: ${scorecard.models_used.join(', ')}.`}
        </p>
        {scorecard.notes !== '' && (
          <p className="mt-1 text-sm text-slate-500">{scorecard.notes}</p>
        )}
      </section>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Tile
          label="Cost per document"
          value={
            summary.data === undefined
              ? 'Loading'
              : formatMicroUsd(summary.data.cost_per_document_microusd)
          }
          note="Provider list price, so a free tier run still shows what the work costs"
        />
        <Tile
          label="Auto accept rate"
          value={summary.data === undefined ? 'Loading' : formatRate(summary.data.auto_accept_rate)}
          note="Share of extracted fields that never reached a human"
        />
        <Tile
          label="Documents processed"
          value={scorecard.documents_processed.toLocaleString('en-US')}
          note={`${scorecard.documents_unprocessable.toLocaleString('en-US')} unprocessable of ${scorecard.documents_total.toLocaleString('en-US')} total`}
        />
        <Tile
          label="Open exceptions"
          value={
            summary.data === undefined
              ? 'Loading'
              : summary.data.open_exception_count.toLocaleString('en-US')
          }
          note="Counted from the database, not from the scorecard"
        />
      </div>

      <section className={`${CARD} overflow-x-auto p-4`} aria-labelledby="accuracy-heading">
        <h2 id="accuracy-heading" className="text-base font-semibold text-slate-900">
          Per field accuracy by tier
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Canonical matches over expected fields, as the scorecard published them. This screen
          computes no rate the scorecard did not publish.
        </p>
        <table className="mt-3 w-full text-left text-sm">
          <caption className="sr-only">
            Canonical matches over expected fields, by family and quality tier
          </caption>
          <thead className="text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium">
                Family
              </th>
              {tiers.map((tier) => (
                <th key={tier} scope="col" className="py-1 pr-3 font-medium">
                  {humanize(tier)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {accuracyFamilies.map((family) => (
              <tr key={family} className="border-t border-slate-100">
                <th scope="row" className="py-1.5 pr-3 font-normal text-slate-700">
                  {humanize(family)}
                </th>
                {tiers.map((tier) => {
                  const cell = cellFor(scorecard.field_accuracy, family, tier)
                  return (
                    <td key={tier} className="py-1.5 pr-3 font-mono text-slate-900">
                      {cell === undefined ? (
                        <span className="text-slate-400">none</span>
                      ) : (
                        <>
                          {cell.correct_canonical} of {cell.fields_expected}
                          <span className="block text-xs text-slate-500">
                            {cell.fields_extracted} extracted
                          </span>
                        </>
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <section className={`${CARD} p-4`} aria-labelledby="reliability-heading">
          <h2 id="reliability-heading" className="text-base font-semibold text-slate-900">
            Reliability diagram
          </h2>
          {calibration.length === 0 ? (
            <p className="mt-2 text-sm text-slate-600">
              This scorecard published no calibration bins.
            </p>
          ) : (
            <div className="mt-3">
              <ReliabilityDiagram bins={calibration} runId={scorecard.run_id} />
            </div>
          )}
        </section>

        <section className={`${CARD} p-4`} aria-labelledby="sweep-heading">
          <h2 id="sweep-heading" className="text-base font-semibold text-slate-900">
            Threshold sweep
          </h2>
          {sweepFamilies.length === 0 ? (
            <p className="mt-2 text-sm text-slate-600">
              This scorecard published no threshold sweep.
            </p>
          ) : (
            <div className="mt-3 space-y-6">
              {sweepFamilies.map((family) => (
                <ThresholdSweep key={family} family={family} points={sweepFor(sweep, family)} />
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
