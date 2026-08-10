/**
 * The exceptions dashboard: the money, ranked by how much of it is at stake.
 *
 * The API ranks by absolute dollar impact and this table renders that order as
 * it arrives. Amounts are integer cents on the wire and become dollars only in
 * the cell that prints them.
 *
 * The listing is paged. A run over the real dataset opens 751 exceptions, and a
 * table that renders all of them at once is thirty thousand pixels of scroll
 * with no way to tell how far down it goes, so the page asks the API for fifty
 * at a time and says which fifty it is showing.
 */

import { Fragment, useState } from 'react'

import { useExceptions, useResolveException, useSummary } from '../api/queries'
import type {
  ExceptionListParams,
  ExceptionRecord,
  ExceptionStatus,
  ExceptionType,
} from '../api/types'
import { EXCEPTION_STATUSES, EXCEPTION_TYPES } from '../api/types'
import { Pager } from '../components/Pager'
import { BUTTON, CARD, FOCUS_RING, SELECT } from '../components/ui'
import { describeDocument } from '../lib/documents'
import { formatCents, formatTimestamp, humanize } from '../lib/format'

const PAGE_SIZE = 50

// Whole dollar floors, kept in cents so nothing here parses a decimal.
const IMPACT_FLOORS: readonly { label: string; cents: number | undefined }[] = [
  { label: 'Any impact', cents: undefined },
  { label: '$100 and up', cents: 100_00 },
  { label: '$1,000 and up', cents: 1_000_00 },
  { label: '$10,000 and up', cents: 10_000_00 },
]

function impactClass(cents: number): string {
  if (cents === 0) {
    return 'text-slate-600'
  }
  return cents < 0 ? 'text-red-700' : 'text-emerald-700'
}

interface DetailProps {
  record: ExceptionRecord
}

function ExceptionDetail({ record }: DetailProps) {
  const [resolution, setResolution] = useState('')
  const resolve = useResolveException()

  return (
    <div className="bg-slate-50 p-4">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="rounded border border-slate-200 bg-white p-3">
          <h4 className="text-sm font-semibold text-slate-900">Statement line</h4>
          <dl className="mt-2 grid grid-cols-2 gap-y-1 text-sm">
            <dt className="text-slate-500">Document</dt>
            <dd className="text-slate-900">
              {record.doc_id === null ? 'none' : describeDocument(record.doc_id).label}
              {record.doc_id !== null && (
                <span className="block font-mono text-xs text-slate-400">{record.doc_id}</span>
              )}
            </dd>
            <dt className="text-slate-500">Line</dt>
            <dd className="font-mono text-slate-900">
              {record.statement_line_no === null ? 'none' : record.statement_line_no}
            </dd>
            <dt className="text-slate-500">Amount</dt>
            <dd className="font-mono text-slate-900">
              {record.statement_amount_cents === null
                ? 'not on the statement'
                : formatCents(record.statement_amount_cents)}
            </dd>
          </dl>
        </div>
        <div className="rounded border border-slate-200 bg-white p-3">
          <h4 className="text-sm font-semibold text-slate-900">Ledger entry</h4>
          <dl className="mt-2 grid grid-cols-2 gap-y-1 text-sm">
            <dt className="text-slate-500">Entry</dt>
            <dd className="font-mono text-slate-900">{record.ledger_entry_id ?? 'none'}</dd>
            <dt className="text-slate-500">Match key</dt>
            <dd className="font-mono text-slate-900">{record.match_key ?? 'none'}</dd>
            <dt className="text-slate-500">Amount</dt>
            <dd className="font-mono text-slate-900">
              {record.ledger_amount_cents === null
                ? 'not in the ledger'
                : formatCents(record.ledger_amount_cents)}
            </dd>
          </dl>
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-[10rem_minmax(0,1fr)]">
        <dt className="text-slate-500">Explanation</dt>
        <dd className="text-slate-900">{record.explanation}</dd>
        <dt className="text-slate-500">Memo amount</dt>
        <dd className="font-mono text-slate-900">{formatCents(record.memo_amount_cents)}</dd>
        <dt className="text-slate-500">Detected</dt>
        <dd className="text-slate-900">{formatTimestamp(record.detected_at)}</dd>
        <dt className="text-slate-500">Run</dt>
        <dd className="font-mono text-slate-900">{record.run_id}</dd>
      </dl>

      {record.status === 'open' && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label htmlFor={`resolution-${record.exception_id}`} className="text-sm text-slate-700">
            Resolution
          </label>
          <input
            id={`resolution-${record.exception_id}`}
            value={resolution}
            onChange={(event) => setResolution(event.target.value)}
            className={`min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm ${FOCUS_RING}`}
          />
          <button
            type="button"
            className={BUTTON}
            disabled={resolution === '' || resolve.isPending}
            onClick={() =>
              resolve.mutate({ exceptionId: record.exception_id, resolution })
            }
          >
            Mark resolved
          </button>
        </div>
      )}
      {resolve.error !== null && (
        <p role="alert" className="mt-2 text-sm text-red-800">
          {resolve.error.message}
        </p>
      )}
    </div>
  )
}

export function Exceptions() {
  const [type, setType] = useState<ExceptionType | ''>('')
  const [status, setStatus] = useState<ExceptionStatus | ''>('')
  const [floor, setFloor] = useState<number | undefined>(undefined)
  const [offset, setOffset] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)

  const params: ExceptionListParams = {
    ...(type === '' ? {} : { type }),
    ...(status === '' ? {} : { status }),
    ...(floor === undefined ? {} : { min_impact_cents: floor }),
    limit: PAGE_SIZE,
    offset,
  }

  const summary = useSummary()
  const exceptions = useExceptions(params)
  const items = exceptions.data?.items ?? []
  const total = exceptions.data?.total ?? 0

  // A narrower filter can leave the current page past the end of the listing, so
  // every filter change goes back to the largest impact.
  const refilter = (change: () => void): void => {
    change()
    setOffset(0)
    setExpanded(null)
  }

  return (
    <div className="space-y-4">
      <section className={`${CARD} p-4`} aria-labelledby="exceptions-heading">
        <h1 id="exceptions-heading" className="text-sm font-medium uppercase tracking-wide text-slate-500">
          Exceptions
        </h1>
        <p className="mt-1 text-3xl font-semibold text-slate-900">
          {summary.data === undefined
            ? 'Loading'
            : `${formatCents(summary.data.gross_dollars_at_risk_cents)} at risk`}
        </p>
        {summary.data !== undefined && (
          <p className="mt-1 text-sm text-slate-600">
            {summary.data.open_exception_count.toLocaleString('en-US')} open exceptions across{' '}
            {summary.data.documents_processed.toLocaleString('en-US')} documents, ranked by how
            many dollars each one moves.
          </p>
        )}
      </section>

      <section className={`${CARD} flex flex-wrap items-end gap-4 p-4`} aria-label="Exception filters">
        <label className="text-sm text-slate-700">
          <span className="mr-2">Type</span>
          <select
            className={SELECT}
            value={type}
            onChange={(event) => {
              refilter(() => setType(event.target.value as ExceptionType | ''))
            }}
          >
            <option value="">Every type</option>
            {EXCEPTION_TYPES.map((value) => (
              <option key={value} value={value}>
                {humanize(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          <span className="mr-2">Status</span>
          <select
            className={SELECT}
            value={status}
            onChange={(event) => {
              refilter(() => setStatus(event.target.value as ExceptionStatus | ''))
            }}
          >
            <option value="">Every status</option>
            {EXCEPTION_STATUSES.map((value) => (
              <option key={value} value={value}>
                {humanize(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          <span className="mr-2">Minimum impact</span>
          <select
            className={SELECT}
            value={floor === undefined ? '' : String(floor)}
            onChange={(event) => {
              refilter(() => {
                setFloor(event.target.value === '' ? undefined : Number(event.target.value))
              })
            }}
          >
            {IMPACT_FLOORS.map((option) => (
              <option key={option.label} value={option.cents === undefined ? '' : option.cents}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </section>

      {exceptions.isError && (
        <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">
          The exceptions could not be loaded. {exceptions.error.message}
        </p>
      )}

      <section className={`${CARD} overflow-hidden`} aria-label="Ranked exceptions">
        <table className="w-full text-left text-sm">
          {/* The page range lives on the pager below and only there, so a reader
              is never left comparing two statements of the same count. */}
          <caption className="p-3 text-left text-sm text-slate-600">
            Ranked by absolute dollar impact, largest first.
          </caption>
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                Rank
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Type
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Document
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Statement
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Ledger
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Impact
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Status
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Detail
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((record, rank) => {
              const open = expanded === record.exception_id
              return (
                <Fragment key={record.exception_id}>
                <tr className="border-t border-slate-100 align-top">
                  <td className="px-3 py-2 font-mono text-slate-500">{offset + rank + 1}</td>
                  <td className="px-3 py-2 text-slate-900">{humanize(record.exception_type)}</td>
                  <td className="px-3 py-2 text-slate-700">
                    <span className="block">
                      {record.doc_id === null ? 'none' : describeDocument(record.doc_id).label}
                      {record.statement_line_no === null
                        ? ''
                        : `, line ${String(record.statement_line_no)}`}
                    </span>
                    {record.doc_id !== null && (
                      <span className="block font-mono text-xs text-slate-400">
                        {record.doc_id}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-slate-900">
                    {record.statement_amount_cents === null
                      ? 'none'
                      : formatCents(record.statement_amount_cents)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-slate-900">
                    {record.ledger_amount_cents === null
                      ? 'none'
                      : formatCents(record.ledger_amount_cents)}
                  </td>
                  <td
                    className={`px-3 py-2 text-right font-mono font-semibold ${impactClass(record.dollar_impact_cents)}`}
                  >
                    {formatCents(record.dollar_impact_cents)}
                    {record.dollar_impact_cents === 0 && record.memo_amount_cents !== 0 && (
                      <span className="block text-xs font-normal text-slate-500">
                        memo {formatCents(record.memo_amount_cents)}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-700">{humanize(record.status)}</td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      className={BUTTON}
                      aria-expanded={open}
                      aria-controls={`exception-detail-${record.exception_id}`}
                      onClick={() => setExpanded(open ? null : record.exception_id)}
                    >
                      {open ? 'Hide' : 'Compare'}
                    </button>
                  </td>
                </tr>
                {open && (
                  <tr>
                    <td colSpan={8} className="p-0" id={`exception-detail-${record.exception_id}`}>
                      <ExceptionDetail record={record} />
                    </td>
                  </tr>
                )}
                </Fragment>
              )
            })}
            {items.length === 0 && !exceptions.isPending && (
              <tr className="border-t border-slate-100">
                <td colSpan={8} className="px-3 py-6 text-slate-500">
                  Nothing matches these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="border-t border-slate-100 p-3">
          <Pager
            offset={offset}
            count={items.length}
            total={total}
            pageSize={PAGE_SIZE}
            noun="exceptions"
            onOffset={(next) => {
              setOffset(next)
              setExpanded(null)
            }}
          />
        </div>
      </section>
    </div>
  )
}
