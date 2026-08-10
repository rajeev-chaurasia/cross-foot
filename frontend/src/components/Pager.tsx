/**
 * One page of a listing, and the two controls that reach the rest of it.
 *
 * Both listings are far larger than a screen: 1,901 fields in the queue and 751
 * exceptions on the dashboard. The API pages both with `limit` and `offset`, and
 * `total` is the count the filter matched rather than the size of the page, so
 * everything printed here is a count the API sent. Nothing is derived beyond
 * putting one of those counts after another.
 */

import { pageRangeLabel } from '../lib/format'
import { BUTTON } from './ui'

const GROUPER = new Intl.NumberFormat('en-US')

export interface PagerProps {
  /** Index of the first row on this page, as sent to the API. */
  offset: number
  /** Rows on this page. */
  count: number
  /** Rows the filter matched, which is what the pages are pages of. */
  total: number
  pageSize: number
  /** Plural noun for what is being paged, for example "fields". */
  noun: string
  onOffset: (offset: number) => void
}

export function Pager({ offset, count, total, pageSize, noun, onOffset }: PagerProps) {
  const page = Math.floor(offset / pageSize) + 1
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const atStart = offset === 0
  const atEnd = offset + count >= total

  return (
    <nav
      aria-label={`${noun} pages`}
      className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-sm text-slate-600"
    >
      <p>{pageRangeLabel(offset, count, total, noun)}</p>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className={BUTTON}
          disabled={atStart}
          onClick={() => {
            onOffset(Math.max(offset - pageSize, 0))
          }}
        >
          Previous page
        </button>
        <span className="px-1">
          Page {GROUPER.format(page)} of {GROUPER.format(pages)}
        </span>
        <button
          type="button"
          className={BUTTON}
          disabled={atEnd}
          onClick={() => {
            onOffset(offset + pageSize)
          }}
        >
          Next page
        </button>
      </div>
    </nav>
  )
}
