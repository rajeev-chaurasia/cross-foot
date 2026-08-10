/**
 * Saying out loud what the reviewer is actually looking at.
 *
 * `crop_kind` is the renderer's own record of how it found the region, written
 * by the render that cut it. All three answers are legitimate: a whole page is
 * the floor the renderer falls back to when a field carries no usable
 * coordinates, not a failure. It only becomes a problem when it is shown without
 * saying so, because a reader looking at an entire statement cannot tell "here
 * is the value" from "we could not find it" by looking.
 *
 * A field with no picture is captioned the same way, from
 * `crop_unavailable_reason`. Only one of those reasons is a fault of anything: a
 * CSV has rows rather than pages, so it was never going to have an image, and
 * telling a reviewer their healthy file could not be read sends them looking for
 * damage that is not there.
 */

import type { CropKind, CropUnavailableReason } from '../api/types'

export interface CropCaption {
  /** A short badge above the image, or in place of it. */
  readonly headline: string
  /** Why the reviewer is looking at this much of the page, or at none of it. */
  readonly detail: string
}

const CAPTIONS: Record<CropKind, CropCaption> = {
  exact_bbox: {
    headline: 'The value itself',
    detail: 'Cut to the box this value was read from, with a little margin around it.',
  },
  row_band: {
    headline: 'The whole row',
    detail:
      'The exact box was not recorded, so this is the row of the table the value sits on.',
  },
  full_page: {
    headline: 'The whole page',
    detail:
      'No position was recorded for this value, so the whole statement is shown. ' +
      'Find the value on the page yourself before accepting it.',
  },
}

const UNAVAILABLE: Record<CropUnavailableReason, CropCaption> = {
  no_page_image: {
    headline: 'This format has no page image',
    detail:
      'The value was read from rows of data rather than from a printed page, so there ' +
      'is nothing to show a picture of. The raw text below is the source.',
  },
  source_missing: {
    headline: 'The source document is not in the dataset',
    detail: 'The value below is what was read from it before it went missing.',
  },
  source_unreadable: {
    headline: 'The source document could not be read',
    detail: 'The file is damaged or too large to render. The value below is still reviewable.',
  },
  source_unreachable: {
    headline: 'The source document could not be located',
    detail: 'Its recorded path does not resolve inside the dataset.',
  },
  page_missing: {
    headline: 'That page is not in the source document',
    detail: 'The value cites a page the document does not have.',
  },
}

export function cropCaption(kind: CropKind): CropCaption {
  return CAPTIONS[kind]
}

export function unavailableCaption(reason: CropUnavailableReason): CropCaption {
  return UNAVAILABLE[reason]
}
