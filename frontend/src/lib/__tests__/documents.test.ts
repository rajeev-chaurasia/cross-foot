import { describe, expect, it } from 'vitest'

import { describeDocument, describeField, lineLabel } from '../documents'

describe('describeDocument', () => {
  it('says which statement, in words a dealership clerk reads', () => {
    const described = describeDocument('doc-floorplan_statement-dlr-meridian-202606-02')
    expect(described.label).toBe('Meridian floorplan statement, June 2026, document 2')
    expect(described.parsed).toBe(true)
  })

  it('keeps the raw id, because that is what a bug report carries', () => {
    const id = 'doc-parts_statement-dlr-kaizen-202604-01'
    expect(describeDocument(id).id).toBe(id)
  })

  it('never prints the primary key as the label', () => {
    const id = 'doc-warranty_credit_memo-dlr-northstar-202512-01'
    const described = describeDocument(id)
    expect(described.label).not.toContain('doc-')
    expect(described.label).not.toContain('dlr-')
    expect(described.label).toBe('Northstar warranty credit memo, December 2025, document 1')
  })

  it('prints an id it cannot parse exactly as it arrived rather than guessing', () => {
    const described = describeDocument('doc-corrupted-truncated-01')
    expect(described.label).toBe('doc-corrupted-truncated-01')
    expect(described.parsed).toBe(false)
  })
})

describe('lineLabel', () => {
  it('calls a null line the header rather than line zero', () => {
    expect(lineLabel(null)).toBe('header')
    expect(lineLabel(7)).toBe('line 7')
  })
})

describe('describeField', () => {
  it('puts the field, the line and the statement in one sentence', () => {
    expect(
      describeField('line_amount', 7, 'doc-floorplan_statement-dlr-meridian-202606-02'),
    ).toBe('Line amount on line 7 of Meridian floorplan statement, June 2026, document 2')
  })

  it('carries no field id, which is the label that used to be shown', () => {
    const sentence = describeField('vin', null, 'doc-parts_statement-dlr-atlas-202607-01')
    expect(sentence).toBe('VIN on header of Atlas parts statement, July 2026, document 1')
    expect(sentence).not.toContain('fld-')
  })
})
