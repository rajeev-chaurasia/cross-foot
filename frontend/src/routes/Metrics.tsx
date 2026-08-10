/**
 * The published numbers, and nothing else.
 *
 * Every figure on this page is read from GET /api/metrics or
 * GET /api/stats/summary. The accuracy grid prints the counts the scorecard
 * published rather than a rate derived from them, because the scorecard does
 * not publish a per cell rate and this screen is not allowed to invent one.
 *
 * The two sources are not the same kind of number and the page says so wherever
 * they sit together. A scorecard figure is a held out result at a fixed
 * threshold, measured once and frozen with a run id. A summary figure is a
 * reading of the database as it stands, over every split, and it moves as
 * reviewing happens. The review rate in particular exists in both senses, so the
 * published one is only ever shown inside the sweep, where the split it was
 * measured on is named on the marker itself.
 */

import { useMetrics, useSummary } from '../api/queries'
import type { FieldAccuracyCell, QualityTier } from '../api/types'
import { QUALITY_TIERS } from '../api/types'
import { ReliabilityDiagram } from '../components/charts/ReliabilityDiagram'
import { ThresholdSweep } from '../components/charts/ThresholdSweep'
import { CARD } from '../components/ui'
import { formatMicroUsd, formatRate, formatTimestamp, humanize } from '../lib/format'
import { familiesOf, sweepFor, THRESHOLD_SPLIT } from '../lib/metrics'

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
          {/* An empty list is an absent record, not an absent model. The scanned
              tier goes through a vision model on every run, so "none were
              called" is a claim this page is never in a position to make. */}
          {scorecard.models_used.length === 0
            ? 'This run did not record the models it called.'
            : `Models: ${scorecard.models_used.join(', ')}.`}{' '}
          {scorecard.documents_processed.toLocaleString('en-US')} documents processed,{' '}
          {scorecard.documents_unprocessable.toLocaleString('en-US')} unprocessable of{' '}
          {scorecard.documents_total.toLocaleString('en-US')} in the split.
        </p>
        {scorecard.notes !== '' && (
          <p className="mt-1 text-sm text-slate-500">{scorecard.notes}</p>
        )}
      </section>

      <section aria-labelledby="database-heading">
        <h2 id="database-heading" className="text-base font-semibold text-slate-900">
          This database as it stands
        </h2>
        {/* Said once, above the tiles, rather than repeated in four notes. These
            are readings taken now, over every split, and three of the four move
            as reviewing happens. Nothing in this row is a published result, and
            the review share in particular is not the review rate the sweep below
            reports on the held out split. */}
        <p className="mt-1 text-sm text-slate-600">
          Counted from the review database, across the train, calibration and test splits
          together. These move as documents are ingested and as reviewers work the queue. The
          scorecard figures below do not: they were measured once, on the split each one names.
        </p>
        <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
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
            value={
              summary.data === undefined ? 'Loading' : formatRate(summary.data.auto_accept_rate)
            }
            note="Share of extracted fields that never reached a human"
          />
          <Tile
            label="In the review queue"
            value={
              summary.data === undefined
                ? 'Loading'
                : formatRate(summary.data.review_queue_share)
            }
            note="Waiting for a human right now, not the published review rate"
          />
          <Tile
            label="Open exceptions"
            value={
              summary.data === undefined
                ? 'Loading'
                : summary.data.open_exception_count.toLocaleString('en-US')
            }
            note="Still unresolved, so this falls as they are worked"
          />
        </div>
      </section>

      <section className={`${CARD} overflow-x-auto p-4`} aria-labelledby="accuracy-heading">
        <h2 id="accuracy-heading" className="text-base font-semibold text-slate-900">
          Per field accuracy by quality tier
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Canonical matches over expected fields, as the scorecard published them. This screen
          computes no rate the scorecard did not publish.
        </p>
        {/* The tier is the only figure on this project that the dataset knows and
            a deployment never would, so it is named as an evaluation axis here
            and appears nowhere in the review surface. */}
        <p className="mt-1 text-sm text-slate-500">
          Quality tier is an evaluation axis, not a product signal. The generator recorded how each
          file was degraded, so accuracy can be read per condition. A statement arriving from a
          manufacturer carries no such label, the confidence model is not given one, and the review
          queue never shows one.
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

      {/* Stacked rather than side by side. The sweep grows a figure per family
          and the reliability diagram never grows at all, so a two column row
          leaves one column ending a thousand pixels above the other. */}
      <div className="space-y-4">
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
            Threshold sweep, {THRESHOLD_SPLIT} split against {scorecard.split} split
          </h2>
          {sweepFamilies.length === 0 ? (
            <p className="mt-2 text-sm text-slate-600">
              This scorecard published no threshold sweep.
            </p>
          ) : (
            <>
              <p className="mt-1 text-sm text-slate-600">
                A threshold may only be chosen on the {THRESHOLD_SPLIT} split, so the curve and the
                chosen point are {THRESHOLD_SPLIT} figures. What the held out {scorecard.split} split
                delivered at that same threshold is the second marker, and it is the number the
                product claim rests on.
              </p>
              <div className="mt-3 grid grid-cols-1 gap-6 lg:grid-cols-2">
                {sweepFamilies.map((family) => (
                  <ThresholdSweep
                    key={family}
                    family={family}
                    points={sweepFor(sweep, family)}
                    reportedSplit={scorecard.split}
                  />
                ))}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
