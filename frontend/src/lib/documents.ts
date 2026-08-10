/**
 * Saying which statement, and which line of it, in words rather than in keys.
 *
 * `doc-floorplan_statement-dlr-meridian-202606-02` is a primary key. It already
 * carries the marque, the kind of paperwork and the month the statement covers,
 * but it carries them in the manifest's vocabulary, and a reviewer working a
 * queue needs the statement rather than the key.
 *
 * This is formatting, not knowledge. Nothing here reads the dataset, the ledger
 * or any answer key: every word in a label is already inside the string the API
 * sent, and an id that does not parse is printed exactly as it arrived rather
 * than guessed at. The raw id stays beside the label everywhere the label is
 * used, because the id is what a bug report has to carry.
 */

import type { FieldName } from '../api/types'
import { humanize } from './format'

/** doc-{doc_type}-dlr-{marque}-{YYYY}{MM}-{sequence}, the only shape ingest writes. */
const DOC_ID = /^doc-([a-z_]+)-dlr-([a-z]+)-(\d{4})(\d{2})-(\d+)$/

const MONTHS: readonly string[] = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

export interface DocumentDescription {
  /** The identifier the API keys on, kept for debugging and for bug reports. */
  readonly id: string
  /** Marque, paperwork and period in words, or the raw id when it will not parse. */
  readonly label: string
  /** False when the label IS the id, so a caller does not print it twice. */
  readonly parsed: boolean
}

function capitalize(word: string): string {
  return word.replace(/^./, (first) => first.toUpperCase())
}

/** One document as a reviewer would name it out loud. */
export function describeDocument(docId: string): DocumentDescription {
  const match = DOC_ID.exec(docId)
  if (match === null) {
    return { id: docId, label: docId, parsed: false }
  }
  const [, docType, marque, year, month, sequence] = match
  const name = MONTHS[Number(month) - 1]
  const period = name === undefined ? `${month}/${year}` : `${name} ${year}`
  const paperwork = docType.split('_').join(' ')
  return {
    id: docId,
    label: `${capitalize(marque)} ${paperwork}, ${period}, document ${Number(sequence)}`,
    parsed: true,
  }
}

/** Where on the statement a field sits. Null is the header, not line zero. */
export function lineLabel(lineNo: number | null): string {
  return lineNo === null ? 'header' : `line ${lineNo}`
}

/** One field in a sentence: what it is, where it sits, and which statement it is on. */
export function describeField(name: FieldName, lineNo: number | null, docId: string): string {
  return `${humanize(name)} on ${lineLabel(lineNo)} of ${describeDocument(docId).label}`
}
