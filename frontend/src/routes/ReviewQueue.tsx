/**
 * The review queue: the uncertain fields, next to the pixels they came from.
 *
 * Everything on this screen is a number the API published. The queue order is
 * the API's total order (ascending confidence, then field id), the confidence is
 * the API's, the queue depth and the extracted field count come from
 * GET /api/stats/summary, and the page counts come from the `total` the queue
 * route returns. Nothing here computes an accuracy.
 *
 * Two things the API knows are deliberately kept off this screen. The document's
 * quality tier is dataset generator metadata: no statement a dealership receives
 * carries one, the confidence model was stripped of it for exactly that reason,
 * and printing it here would imply knowledge the system does not have. `Route`
 * says the honest, artifact-derived version of the same thing, because the
 * router decides it from the file's own bytes. The tier survives only on the
 * metrics page, as an evaluation axis, labelled as one.
 */

import { useEffect, useRef, useState } from 'react'

import { useReviewItem, useReviewQueue, useReviewWrite, useSummary } from '../api/queries'
import type { FieldFamily, ReviewItem, ReviewStatus, ReviewQueueParams } from '../api/types'
import { FIELD_FAMILIES, REVIEW_STATUSES } from '../api/types'
import { Pager } from '../components/Pager'
import { SignalBreakdown } from '../components/SignalBreakdown'
import { ShortcutsBar, ShortcutsOverlay } from '../components/Shortcuts'
import { BUTTON, CARD, FOCUS_RING, KBD, PRIMARY_BUTTON, SELECT } from '../components/ui'
import { cropCaption } from '../lib/crops'
import { describeDocument, describeField, lineLabel } from '../lib/documents'
import { formatConfidence, formatRate, formatShare, humanize } from '../lib/format'

const PAGE_SIZE = 50

const STATUS_CLASS: Record<ReviewStatus, string> = {
  needs_review: 'bg-amber-100 text-amber-900',
  auto_accepted: 'bg-emerald-100 text-emerald-900',
  human_accepted: 'bg-sky-100 text-sky-900',
  human_corrected: 'bg-violet-100 text-violet-900',
}

/** Which end of a freshly loaded page the reviewer should land on. */
type QueueEdge = 'first' | 'last'

export function ReviewQueue() {
  const [status, setStatus] = useState<ReviewStatus | ''>('needs_review')
  const [family, setFamily] = useState<FieldFamily | ''>('')
  const [offset, setOffset] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [reviewer, setReviewer] = useState('')
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const [cropBroken, setCropBroken] = useState(false)
  const [cropZoomed, setCropZoomed] = useState(false)

  const params: ReviewQueueParams = {
    limit: PAGE_SIZE,
    offset,
    ...(status === '' ? {} : { status }),
    ...(family === '' ? {} : { family }),
  }

  const summary = useSummary()
  const queue = useReviewQueue(params)
  const { accept, correct } = useReviewWrite()

  const pageItems = queue.data?.items
  const items = pageItems ?? []
  const total = queue.data?.total ?? 0

  const fallbackIndex = useRef(0)
  const pendingEdge = useRef<QueueEdge | null>(null)
  const correctionRef = useRef<HTMLInputElement>(null)
  const reviewerRef = useRef<HTMLInputElement>(null)
  const selectedRef = useRef<HTMLLIElement>(null)

  let index = items.findIndex((item) => item.field_id === selectedId)
  if (index < 0) {
    index = items.length === 0 ? -1 : Math.min(fallbackIndex.current, items.length - 1)
  }
  const current: ReviewItem | undefined = index >= 0 ? items[index] : undefined

  // Only a page that actually has rows can say where the reviewer is in it. A
  // page still loading must not overwrite the landing the page turn asked for.
  useEffect(() => {
    if (index >= 0) {
      fallbackIndex.current = index
    }
  }, [index])

  // A filter change starts the queue over at its least trusted field.
  useEffect(() => {
    setSelectedId(null)
    fallbackIndex.current = 0
    pendingEdge.current = null
  }, [status, family])

  // A page turn lands on the end the reviewer arrived from, so j and k retrace
  // the same path across a boundary instead of both landing at the top.
  useEffect(() => {
    const edge = pendingEdge.current
    if (edge === null || pageItems === undefined || pageItems.length === 0) {
      return
    }
    pendingEdge.current = null
    const landing = edge === 'last' ? pageItems.length - 1 : 0
    fallbackIndex.current = landing
    setSelectedId(pageItems[landing].field_id)
  }, [pageItems])

  const detail = useReviewItem(current?.field_id ?? null)

  const currentId = current?.field_id ?? null
  const currentValue = current?.value ?? ''
  useEffect(() => {
    setDraft(currentValue)
    setCropBroken(false)
    setCropZoomed(false)
  }, [currentId, currentValue])

  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [currentId])

  const turnPage = (nextOffset: number, edge: QueueEdge): void => {
    pendingEdge.current = edge
    setSelectedId(null)
    fallbackIndex.current = edge === 'last' ? Number.MAX_SAFE_INTEGER : 0
    setOffset(nextOffset)
  }

  const move = (delta: number): void => {
    if (items.length === 0) {
      return
    }
    const next = index + delta
    if (next < 0) {
      if (offset > 0) {
        turnPage(Math.max(offset - PAGE_SIZE, 0), 'last')
      }
      return
    }
    if (next >= items.length) {
      if (offset + items.length < total) {
        turnPage(offset + PAGE_SIZE, 'first')
      }
      return
    }
    const target = items[next]
    if (target !== undefined) {
      setSelectedId(target.field_id)
    }
  }

  const advance = (): void => {
    if (index >= 0 && index + 1 < items.length) {
      const target = items[index + 1]
      if (target !== undefined) {
        setSelectedId(target.field_id)
      }
    }
  }

  const acceptCurrent = (): void => {
    if (current === undefined) {
      return
    }
    accept.mutate(current.field_id)
    advance()
  }

  const namedReviewer = reviewer.trim()
  const reviewerMissing = namedReviewer === ''

  const saveCorrection = (): void => {
    if (current === undefined || draft === '') {
      return
    }
    // A correction is attributed to whoever made it, so it does not get saved
    // under a placeholder.
    if (reviewerMissing) {
      reviewerRef.current?.focus()
      return
    }
    correct.mutate({ fieldId: current.field_id, value: draft, reviewer: namedReviewer })
    advance()
  }

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      if (event.ctrlKey || event.metaKey || event.altKey) {
        return
      }
      const target = event.target as HTMLElement | null
      const typing =
        target !== null &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)

      if (typing) {
        if (event.key === 'Enter' && target === correctionRef.current) {
          event.preventDefault()
          saveCorrection()
        } else if (event.key === 'Escape') {
          event.preventDefault()
          correctionRef.current?.blur()
        }
        return
      }

      switch (event.key) {
        case 'j':
          event.preventDefault()
          move(1)
          break
        case 'k':
          event.preventDefault()
          move(-1)
          break
        case 'a':
          event.preventDefault()
          acceptCurrent()
          break
        case 'c':
          event.preventDefault()
          correctionRef.current?.focus()
          correctionRef.current?.select()
          break
        case '?':
          event.preventDefault()
          setShortcutsOpen((open) => !open)
          break
        case 'Escape':
          setShortcutsOpen(false)
          break
        default:
          break
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
    }
  })

  const writeError = accept.error ?? correct.error
  const place = offset + index + 1
  const position =
    current === undefined
      ? 'The queue is empty.'
      : `Field ${place} of ${total}. ` +
        `${describeField(current.name, current.line_no, current.doc_id)}. ` +
        `Confidence ${formatConfidence(current.confidence)}. Status ${humanize(current.status)}.`

  const sourceDoc = current === undefined ? undefined : describeDocument(current.doc_id)
  const caption = detail.data === undefined ? undefined : cropCaption(detail.data.crop_kind)

  return (
    <div className="space-y-4">
      <ShortcutsOverlay open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />

      <section className={`${CARD} p-4`} aria-labelledby="queue-claim">
        <div className="flex flex-wrap items-baseline justify-between gap-4">
          <div>
            <h1 id="queue-claim" className="text-sm font-medium uppercase tracking-wide text-slate-500">
              Review queue
            </h1>
            <p className="mt-1 text-3xl font-semibold text-slate-900">
              {summary.data === undefined
                ? 'Loading'
                : `Reviewing ${formatShare(summary.data.review_queue_depth, summary.data.fields_extracted)} of fields`}
            </p>
            {summary.data !== undefined && (
              <p className="mt-1 text-sm text-slate-600">
                {summary.data.review_queue_depth.toLocaleString('en-US')} of{' '}
                {summary.data.fields_extracted.toLocaleString('en-US')} extracted fields need a
                human. {formatRate(summary.data.auto_accept_rate)} were auto accepted across{' '}
                {summary.data.documents_processed.toLocaleString('en-US')} documents.
              </p>
            )}
          </div>
          <div className="text-right text-sm text-slate-600">
            <p>
              Page <span className="font-mono">{Math.floor(offset / PAGE_SIZE) + 1}</span> of{' '}
              <span className="font-mono">
                {Math.max(1, Math.ceil(total / PAGE_SIZE)).toLocaleString('en-US')}
              </span>
              , <span className="font-mono">{total.toLocaleString('en-US')}</span> matching fields
            </p>
            <p className="mt-1">Least trusted field first</p>
          </div>
        </div>
        <div className="mt-3 border-t border-slate-100 pt-3">
          <ShortcutsBar />
        </div>
      </section>

      <section className={`${CARD} flex flex-wrap items-end gap-4 p-4`} aria-label="Queue filters">
        <label className="text-sm text-slate-700">
          <span className="mr-2">Status</span>
          <select
            className={SELECT}
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as ReviewStatus | '')
              setOffset(0)
            }}
          >
            <option value="">Every field</option>
            {REVIEW_STATUSES.map((value) => (
              <option key={value} value={value}>
                {humanize(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          <span className="mr-2">Family</span>
          <select
            className={SELECT}
            value={family}
            onChange={(event) => {
              setFamily(event.target.value as FieldFamily | '')
              setOffset(0)
            }}
          >
            <option value="">Every family</option>
            {FIELD_FAMILIES.map((value) => (
              <option key={value} value={value}>
                {humanize(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          <span className="mr-2">Reviewer</span>
          <input
            ref={reviewerRef}
            className={SELECT}
            value={reviewer}
            placeholder="Your name"
            aria-describedby="reviewer-note"
            onChange={(event) => setReviewer(event.target.value)}
          />
        </label>
        <p id="reviewer-note" className="text-xs text-slate-500">
          A correction is saved under this name.
        </p>
      </section>

      <p aria-live="polite" className="sr-only">
        {position}
      </p>

      {writeError !== null && writeError !== undefined && (
        <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">
          {writeError.message}
        </p>
      )}

      {queue.isError && (
        <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">
          The queue could not be loaded. {queue.error.message}
        </p>
      )}

      <div className="grid grid-cols-1 items-stretch gap-4 xl:grid-cols-[15rem_minmax(0,1.35fr)_minmax(0,1fr)]">
        <nav className={`${CARD} flex flex-col p-2`} aria-label="Queue">
          {current !== undefined && total > 0 && (
            <div className="px-1 pb-2">
              <p className="text-xs text-slate-600">
                Field {place.toLocaleString('en-US')} of {total.toLocaleString('en-US')}
              </p>
              <div
                role="progressbar"
                aria-label="Position in the review queue"
                aria-valuemin={1}
                aria-valuemax={total}
                aria-valuenow={place}
                className="mt-1 h-1.5 w-full overflow-hidden rounded bg-slate-100"
              >
                <div
                  className="h-full rounded bg-sky-600"
                  style={{ width: `${String((place / total) * 100)}%` }}
                />
              </div>
            </div>
          )}
          {/* Grows into the height of the row rather than leaving a card of
              empty white under ten items, and stays capped so a page of fifty
              cannot decide the height of the whole screen. */}
          <ol className="max-h-[28rem] space-y-1 overflow-y-auto xl:max-h-[56rem] xl:min-h-0 xl:flex-1">
            {items.map((item, itemIndex) => {
              const selected = itemIndex === index
              return (
                <li key={item.field_id} ref={selected ? selectedRef : null}>
                  <button
                    type="button"
                    aria-current={selected ? 'true' : undefined}
                    onClick={() => setSelectedId(item.field_id)}
                    className={`w-full rounded px-2 py-1.5 text-left text-sm ${FOCUS_RING} ${
                      selected ? 'bg-sky-100 text-sky-950' : 'text-slate-700 hover:bg-slate-50'
                    }`}
                  >
                    <span className="block font-medium">
                      {offset + itemIndex + 1}. {humanize(item.name)}
                    </span>
                    <span className="block text-xs text-slate-500">
                      <span className="font-mono">{formatConfidence(item.confidence)}</span>{' '}
                      confidence, {lineLabel(item.line_no)}
                    </span>
                  </button>
                </li>
              )
            })}
            {items.length === 0 && !queue.isPending && (
              <li className="px-2 py-4 text-sm text-slate-500">Nothing matches these filters.</li>
            )}
          </ol>
          <div className="mt-2 border-t border-slate-100 pt-2">
            <Pager
              offset={offset}
              count={items.length}
              total={total}
              pageSize={PAGE_SIZE}
              noun="fields"
              onOffset={(next) => {
                turnPage(next, 'first')
              }}
            />
          </div>
        </nav>

        <section className={`${CARD} flex flex-col p-4`} aria-label="Source crop">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
              Source crop
            </h2>
            {current !== undefined && !cropBroken && (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className={BUTTON}
                  aria-pressed={cropZoomed}
                  onClick={() => {
                    setCropZoomed((zoomed) => !zoomed)
                  }}
                >
                  {cropZoomed ? 'Fit to the panel' : 'Zoom to full size'}
                </button>
                <a
                  href={current.crop_url}
                  target="_blank"
                  rel="noreferrer"
                  className={BUTTON}
                >
                  Open in a new tab
                </a>
              </div>
            )}
          </div>
          {current === undefined ? (
            <p className="mt-3 text-sm text-slate-500">No field selected.</p>
          ) : (
            <>
              {caption !== undefined && (
                <p className="mt-2 text-sm text-slate-700">
                  <span className="font-medium">{caption.headline}.</span> {caption.detail}
                </p>
              )}
              {/* The whole premise of this screen is reading the value beside
                  the pixels it came from, so the image gets the height of the
                  row rather than a thumbnail's worth of it. */}
              <div
                className={`mt-2 h-[30rem] rounded border border-slate-200 bg-slate-50 p-2 xl:h-auto xl:min-h-[34rem] xl:flex-1 ${
                  cropZoomed ? 'overflow-auto' : 'flex items-center justify-center'
                }`}
              >
                {cropBroken ? (
                  <p className="p-6 text-center text-sm text-slate-500">
                    The crop for this field is not available.
                  </p>
                ) : (
                  <img
                    src={current.crop_url}
                    alt={`Source crop for ${describeField(current.name, current.line_no, current.doc_id)}`}
                    className={
                      cropZoomed
                        ? 'max-w-none'
                        : 'mx-auto max-h-full w-auto max-w-full object-contain'
                    }
                    onError={() => setCropBroken(true)}
                  />
                )}
              </div>
              <dl className="mt-3 grid grid-cols-[8rem_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
                <dt className="text-slate-500">Document</dt>
                <dd className="text-slate-900">
                  <span className="block">{sourceDoc?.label}</span>
                  {sourceDoc?.parsed === true && (
                    <span className="block font-mono text-xs text-slate-400" title={sourceDoc.id}>
                      {sourceDoc.id}
                    </span>
                  )}
                </dd>
                <dt className="text-slate-500">Raw text</dt>
                <dd className="font-mono text-slate-900">{current.raw_text ?? 'none'}</dd>
                {detail.data !== undefined && (
                  <>
                    <dt className="text-slate-500">Document type</dt>
                    <dd className="text-slate-900">
                      {detail.data.document.doc_type === null
                        ? 'unknown'
                        : humanize(detail.data.document.doc_type)}
                    </dd>
                    <dt className="text-slate-500">Route</dt>
                    <dd className="text-slate-900">{humanize(detail.data.document.route)}</dd>
                  </>
                )}
              </dl>
            </>
          )}
        </section>

        <section className={`${CARD} p-4`} aria-label="Extracted value">
          {current === undefined ? (
            <p className="text-sm text-slate-500">
              {queue.isPending ? 'Loading the queue.' : 'The queue is empty.'}
            </p>
          ) : (
            <>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-lg font-semibold text-slate-900">{humanize(current.name)}</h2>
                <span className={`rounded px-2 py-0.5 text-xs ${STATUS_CLASS[current.status]}`}>
                  {humanize(current.status)}
                </span>
              </div>
              <p className="text-sm text-slate-600">
                {humanize(current.family)} field on {lineLabel(current.line_no)} of{' '}
                {sourceDoc?.label}
              </p>
              <p className="font-mono text-xs text-slate-400" title={current.field_id}>
                {current.field_id}
              </p>

              <div className="mt-4 flex items-baseline gap-6">
                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-500">Extracted value</p>
                  <p className="font-mono text-2xl text-slate-900">{current.value ?? 'none'}</p>
                </div>
                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-500">Confidence</p>
                  <p className="font-mono text-2xl text-slate-900">
                    {formatConfidence(current.confidence)}
                  </p>
                </div>
              </div>

              {detail.data !== undefined && (
                <SignalBreakdown
                  signals={detail.data.signals}
                  family={detail.data.family}
                  name={detail.data.name}
                />
              )}

              <div className="mt-4 border-t border-slate-100 pt-4">
                <label htmlFor="correction" className="block text-sm font-medium text-slate-800">
                  Correction <kbd className={KBD}>c</kbd>
                </label>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <input
                    id="correction"
                    ref={correctionRef}
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    className={`min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1.5 font-mono text-sm ${FOCUS_RING}`}
                  />
                  <button
                    type="button"
                    className={PRIMARY_BUTTON}
                    disabled={reviewerMissing}
                    onClick={saveCorrection}
                  >
                    Save correction
                  </button>
                  <button type="button" className={BUTTON} onClick={acceptCurrent}>
                    Accept <kbd className={KBD}>a</kbd>
                  </button>
                </div>
                {reviewerMissing && (
                  <p className="mt-2 text-sm text-slate-600">
                    Put your name in the Reviewer box above before saving a correction.
                  </p>
                )}
              </div>

              {detail.data !== undefined && detail.data.neighbors.length > 0 && (
                <div className="mt-4 border-t border-slate-100 pt-4">
                  <h3 className="text-sm font-semibold text-slate-900">The rest of this line</h3>
                  <table className="mt-2 w-full text-left text-sm">
                    <thead>
                      <tr className="text-xs uppercase tracking-wide text-slate-500">
                        <th scope="col" className="py-1 font-medium">
                          Field
                        </th>
                        <th scope="col" className="py-1 font-medium">
                          Value
                        </th>
                        <th scope="col" className="py-1 font-medium">
                          Confidence
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.data.neighbors.map((neighbor) => (
                        <tr key={neighbor.field_id} className="border-t border-slate-100">
                          <th scope="row" className="py-1 pr-3 font-normal text-slate-700">
                            {humanize(neighbor.name)}
                          </th>
                          <td className="py-1 pr-3 font-mono text-slate-900">
                            {neighbor.value ?? 'none'}
                          </td>
                          <td className="py-1 font-mono text-slate-600">
                            {formatConfidence(neighbor.confidence)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  )
}
