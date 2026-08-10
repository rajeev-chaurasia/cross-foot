/**
 * Formatting at the edge only.
 *
 * Money arrives as integer cents and LLM cost as integer microusd, and it stays
 * integral right up to the string that gets painted. Nothing here rounds a
 * dollar amount through a float, and nothing here derives a figure the API did
 * not publish.
 */

const GROUPER = new Intl.NumberFormat('en-US')

/** Integer cents to a dollar string, with the sign in front of the currency. */
export function formatCents(cents: number): string {
  const negative = cents < 0
  const magnitude = Math.abs(Math.trunc(cents))
  const dollars = Math.floor(magnitude / 100)
  const remainder = magnitude - dollars * 100
  const grouped = GROUPER.format(dollars)
  return `${negative ? '-' : ''}$${grouped}.${String(remainder).padStart(2, '0')}`
}

/** Integer cents to a dollar string with no sign, for a magnitude column. */
export function formatCentsAbsolute(cents: number): string {
  return formatCents(Math.abs(cents))
}

/**
 * Integer microusd to a dollar string.
 *
 * Cost per document lands in fractions of a cent on a free tier run, so this
 * keeps six digits of fraction and trims the trailing zeros back to at least
 * two, which is enough to show that 45000 microusd is 4.5 cents of work.
 */
export function formatMicroUsd(microusd: number): string {
  const negative = microusd < 0
  const magnitude = Math.abs(Math.trunc(microusd))
  const dollars = Math.floor(magnitude / 1_000_000)
  const remainder = magnitude - dollars * 1_000_000
  const fraction = String(remainder).padStart(6, '0').replace(/0+$/, '').padEnd(2, '0')
  return `${negative ? '-' : ''}$${GROUPER.format(dollars)}.${fraction}`
}

/** A 0..1 rate as a percentage string. The rate itself comes from the API. */
export function formatRate(rate: number, digits = 1): string {
  return `${(rate * 100).toFixed(digits)}%`
}

/**
 * A count over a total as a percentage string.
 *
 * Both arguments are counts the API published; this only puts them over each
 * other so the queue can say what share of all fields it holds. A zero total
 * has no share, so it reads as a dash rather than as zero percent.
 */
export function formatShare(count: number, total: number, digits = 1): string {
  if (total <= 0) {
    return '-'
  }
  return formatRate(count / total, digits)
}

/**
 * A confidence in 0..1 as a percentage.
 *
 * The API sends the full float, and a reviewer reading "97 percent auto accept
 * precision" three panels away should not have to switch units to read the
 * number in front of them. Same digits as every other rate on screen.
 */
export function formatConfidence(confidence: number): string {
  return formatRate(confidence, 1)
}

/**
 * A confidence threshold as a fixed number of places.
 *
 * A threshold is chosen by a search over a grid and lands on a float like
 * 0.9299335323598775. Four places identify the point without pretending the
 * remaining digits mean anything, and it is the same number of places the
 * figure captions print, so the two never disagree.
 */
export function formatThreshold(threshold: number): string {
  return threshold.toFixed(4)
}

/**
 * "Showing 1 to 50 of 1,901 fields", from counts the API sent.
 *
 * The offset and the page size are what the UI asked for and the total is what
 * the filter matched, so nothing here is derived beyond putting one count after
 * another. A page with no rows says so rather than reading "1 to 0".
 */
export function pageRangeLabel(
  offset: number,
  count: number,
  total: number,
  noun: string,
): string {
  if (count === 0) {
    return `No ${noun} to show`
  }
  return (
    `Showing ${GROUPER.format(offset + 1)} to ${GROUPER.format(offset + count)} ` +
    `of ${GROUPER.format(total)} ${noun}`
  )
}

/** snake_case wire vocabulary to something a dealership CFO reads without help. */
export function humanize(value: string): string {
  return value
    .split('_')
    .map((word) => (word === 'vin' || word === 'ro' ? word.toUpperCase() : word))
    .join(' ')
    .replace(/^./, (first) => first.toUpperCase())
}

/** An ISO timestamp as a short readable date, or the raw string if unparseable. */
export function formatTimestamp(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) {
    return iso
  }
  return parsed.toISOString().replace('T', ' ').slice(0, 16) + ' UTC'
}
