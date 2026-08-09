import { describe, expect, it } from 'vitest'

import {
  formatCents,
  formatCentsAbsolute,
  formatConfidence,
  formatMicroUsd,
  formatRate,
  formatShare,
  humanize,
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

  it('puts a queue depth over the extracted field count', () => {
    expect(formatShare(490, 2_500)).toBe('19.6%')
  })

  it('refuses to divide by an empty corpus', () => {
    expect(formatShare(0, 0)).toBe('-')
  })

  it('keeps the confidence the API sent', () => {
    expect(formatConfidence(0.2)).toBe('0.20')
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
})
