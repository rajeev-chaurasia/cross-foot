/**
 * Turning FieldSignals into sentences a reviewer can act on.
 *
 * The confidence itself is the API's number and is never recomputed here. What
 * this file does is name the evidence behind it, so the screen answers "why is
 * this field in front of me" instead of showing a bare score.
 *
 * Every label is written for the person working the queue, which in a dealership
 * is a clerk and not the engineer who named the signal. The wire name travels
 * beside the label as `code` rather than disappearing, so an engineer reading the
 * same screen can still map a row back to `FieldSignals`.
 */

import type { FieldFamily, FieldName, FieldSignals } from '../api/types'
import { humanize } from './format'

export type SignalVerdict = 'pass' | 'fail' | 'unavailable' | 'info'

export interface SignalRow {
  key: string
  /** What the signal means, in the reviewer's words. */
  label: string
  /** The name this signal has in FieldSignals, kept so the two can be mapped. */
  code: string
  display: string
  verdict: SignalVerdict
  note: string
}

function scoreVerdict(value: number | null): SignalVerdict {
  if (value === null) {
    return 'unavailable'
  }
  return value >= 1 ? 'pass' : 'fail'
}

function display(value: number | null): string {
  return value === null ? 'not available' : value.toFixed(2)
}

function validatorNote(family: FieldFamily, name: FieldName, value: number | null): string {
  if (value === null) {
    return 'No format check covers this kind of field.'
  }
  if (value >= 1) {
    return 'The format check accepted this value.'
  }
  if (name === 'vin') {
    return 'VIN check digit failed, so at least one character was read wrong.'
  }
  if (family === 'reference') {
    return 'The format check rejected this reference number.'
  }
  if (family === 'amount') {
    return 'This value could not be read as an amount.'
  }
  if (family === 'date') {
    return 'This value could not be read as a date.'
  }
  return 'The format check rejected this value.'
}

/** One row per signal, in the order a reviewer reads them. */
export function signalRows(
  signals: FieldSignals,
  family: FieldFamily,
  name: FieldName,
): SignalRow[] {
  const rows: SignalRow[] = [
    {
      key: 'self_consistency',
      label: 'Read twice, same answer',
      code: 'self_consistency',
      display: display(signals.self_consistency),
      verdict: scoreVerdict(signals.self_consistency),
      note:
        signals.self_consistency === null
          ? 'The field was read once, so there is nothing to compare.'
          : signals.self_consistency >= 1
            ? 'Repeat readings of the crop agreed.'
            : 'Repeat readings of the crop disagreed.',
    },
    {
      key: 'det_llm_agreement',
      label: 'The two readings agree',
      code: 'det_llm_agreement',
      display: display(signals.det_llm_agreement),
      verdict: scoreVerdict(signals.det_llm_agreement),
      note:
        signals.det_llm_agreement === null
          ? 'Only one extractor could see this field.'
          : signals.det_llm_agreement >= 1
            ? 'The embedded text and the vision model read the same value.'
            : 'The embedded text and the vision model read different values.',
    },
    {
      key: 'validator_pass',
      label: 'Passes the format check',
      code: 'validator_pass',
      display: display(signals.validator_pass),
      verdict: scoreVerdict(signals.validator_pass),
      note: validatorNote(family, name, signals.validator_pass),
    },
    {
      key: 'grammar_match',
      label: "Matches the manufacturer's number format",
      code: 'grammar_match',
      display: display(signals.grammar_match),
      verdict: scoreVerdict(signals.grammar_match),
      note:
        signals.grammar_match === null
          ? 'This kind of field has no manufacturer number format to match.'
          : signals.grammar_match >= 1
            ? "The value matches the manufacturer's reference format."
            : "The value does not match the manufacturer's reference format.",
    },
    {
      key: 'crossfoot_ok',
      label: 'Column adds to the printed total',
      code: 'crossfoot_ok',
      display: display(signals.crossfoot_ok),
      verdict: scoreVerdict(signals.crossfoot_ok),
      note:
        signals.crossfoot_ok === null
          ? 'This document has no total to add the column against.'
          : signals.crossfoot_ok >= 1
            ? 'The amount column adds to the printed total.'
            : 'The amount column does not add to the printed total.',
    },
    {
      key: 'crossfoot_residual_suspect',
      label: 'Blamed for the column not adding up',
      code: 'crossfoot_residual_suspect',
      display: signals.crossfoot_residual_suspect ? 'suspect' : 'clear',
      verdict: signals.crossfoot_residual_suspect ? 'fail' : 'pass',
      note: signals.crossfoot_residual_suspect
        ? 'The amount the column is out by points at this line.'
        : 'The amount the column is out by does not point at this line.',
    },
    {
      key: 'char_ambiguity',
      label: 'Easily confused characters',
      code: 'char_ambiguity',
      display: signals.char_ambiguity.toFixed(2),
      verdict: signals.char_ambiguity > 0 ? 'fail' : 'pass',
      note:
        signals.char_ambiguity > 0
          ? 'Characters in this value are the ones scanners confuse, such as 0 against O and 1 against I.'
          : 'No easily confused characters in this value.',
    },
    {
      key: 'route',
      label: 'How this file was read',
      code: 'route',
      display: signals.route === null ? 'not available' : humanize(signals.route),
      verdict: 'info',
      note: 'The extractor this document was routed to, decided from its own bytes.',
    },
  ]
  return rows
}

/** The notes behind every failing signal, which is the reason it was flagged. */
export function flagReasons(rows: SignalRow[]): string[] {
  return rows.filter((row) => row.verdict === 'fail').map((row) => row.note)
}
