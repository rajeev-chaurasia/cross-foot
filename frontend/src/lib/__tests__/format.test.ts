import { describe, expect, it } from 'vitest'

import {
  formatCents,
  formatCentsAbsolute,
  formatConfidence,
  formatMicroUsd,
  formatRate,
  formatThreshold,
  humanize,
  pageRangeLabel,
} from '../format'

describe('formatCents', () => {
  it('renders integer cents as grouped dollars', () => {
    expect(formatCents(123_456)).toBe('$1,234.56')
    expect(formatCents(4_400)).toBe('$44.00')
    expect(formatCents(250_000)).toBe('$2,500.00')
  })

  it('puts the sign in front of the currency symbol', () => {
    expect(formatCents(-45_000)).toBe('-$450.00')
    expect(formatCents(-60_000)).toBe('-$600.00')
  })

  it('keeps both cent digits on small and zero amounts', () => {
    expect(formatCents(5)).toBe('$0.05')
    expect(formatCents(0)).toBe('$0.00')
  })

  it('drops the sign for a magnitude column', () => {
    expect(formatCentsAbsolute(-45_000)).toBe('$450.00')
  })
})

describe('formatMicroUsd', () => {
  it('shows a fraction of a cent per document', () => {
    expect(formatMicroUsd(45_000)).toBe('$0.045')
  })

  it('keeps two digits when the fraction is whole dollars', () => {
    expect(formatMicroUsd(1_000_000)).toBe('$1.00')
    expect(formatMicroUsd(0)).toBe('$0.00')
  })
})

describe('rates and shares', () => {
  it('formats a published rate', () => {
    expect(formatRate(0.804)).toBe('80.4%')
    expect(formatRate(0.9964, 2)).toBe('99.64%')
  })

  // A confidence used to print as `0.25` beside three panels of percentages.
  // Same units everywhere is the point, not the extra digit.
  it('reads a confidence in the same units as every other rate on screen', () => {
    expect(formatConfidence(0.2)).toBe('20.0%')
    expect(formatConfidence(1)).toBe('100.0%')
  })

  it('rounds the float the API really sends rather than printing all of it', () => {
    expect(formatConfidence(0.24989915380614763)).toBe('25.0%')
  })
})

describe('formatThreshold', () => {
  it('cuts a searched threshold to four places', () => {
    expect(formatThreshold(0.9299335323598775)).toBe('0.9299')
    expect(formatThreshold(0.6864508663860327)).toBe('0.6865')
  })

  it('pads a round threshold out to the same width', () => {
    expect(formatThreshold(0.7)).toBe('0.7000')
    expect(formatThreshold(0)).toBe('0.0000')
  })
})

describe('pageRangeLabel', () => {
  it('says which slice of the whole listing is on screen', () => {
    expect(pageRangeLabel(0, 50, 1901, 'fields')).toBe('Showing 1 to 50 of 1,901 fields')
    expect(pageRangeLabel(700, 51, 751, 'exceptions')).toBe(
      'Showing 701 to 751 of 751 exceptions',
    )
  })

  it('refuses to read 1 to 0 on an empty page', () => {
    expect(pageRangeLabel(0, 0, 0, 'fields')).toBe('No fields to show')
  })
})

describe('humanize', () => {
  it('turns wire vocabulary into words', () => {
    expect(humanize('needs_review')).toBe('Needs review')
    expect(humanize('missing_from_ledger')).toBe('Missing from ledger')
    expect(humanize('scan_heavy')).toBe('Scan heavy')
  })

  it('leaves initialisms alone', () => {
    expect(humanize('vin')).toBe('VIN')
    expect(humanize('ro_number')).toBe('RO number')
  })

  // "Route: Xlsx" and "How this file was read: Csv" were on the screen a
  // dealership clerk works all day.
  it('spells the file formats the way a reader writes them', () => {
    expect(humanize('csv')).toBe('CSV')
    expect(humanize('xlsx')).toBe('XLSX')
    expect(humanize('digital_pdf')).toBe('Digital PDF')
    expect(humanize('scanned_pdf')).toBe('Scanned PDF')
  })

  it('leaves ordinary words in sentence case', () => {
    expect(humanize('unprocessable')).toBe('Unprocessable')
    expect(humanize('clean_digital')).toBe('Clean digital')
  })
})
