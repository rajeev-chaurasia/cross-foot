/**
 * Saying out loud what the reviewer is actually looking at.
 *
 * `crop_kind` is the renderer's own record of how it found the region, written
 * after it cut the image rather than before. All three answers are legitimate:
 * a whole page is the floor the renderer falls back to when a field carries no
 * usable coordinates, not a failure. It only becomes a problem when it is shown
 * without saying so, because a reader looking at an entire statement cannot tell
 * "here is the value" from "we could not find it" by looking.
 */

import type { CropKind } from '../api/types'

export interface CropCaption {
  /** A short badge above the image. */
  readonly headline: string
  /** Why the reviewer is looking at this much of the page. */
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

export function cropCaption(kind: CropKind): CropCaption {
  return CAPTIONS[kind]
}
