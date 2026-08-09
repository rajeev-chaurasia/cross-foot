/** Shared class strings, so the accessibility floor is written once. */

// Plain :focus rather than :focus-visible, because the review queue moves focus
// from the keyboard and the ring has to show every time it does.
export const FOCUS_RING =
  'focus:outline-2 focus:outline-offset-2 focus:outline-sky-500 focus:rounded-sm'

export const CARD = 'rounded-lg border border-slate-200 bg-white'

export const BUTTON =
  `inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 ` +
  `text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50 ${FOCUS_RING}`

export const PRIMARY_BUTTON =
  `inline-flex items-center gap-2 rounded-md bg-sky-700 px-3 py-1.5 text-sm font-medium ` +
  `text-white hover:bg-sky-800 disabled:opacity-50 ${FOCUS_RING}`

export const SELECT =
  `rounded-md border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 ${FOCUS_RING}`

export const KBD =
  'rounded border border-slate-300 bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-700'
