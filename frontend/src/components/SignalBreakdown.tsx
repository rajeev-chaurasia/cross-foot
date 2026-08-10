/** The evidence behind one field's confidence, with the failures called out. */

import type { FieldFamily, FieldName, FieldSignals } from '../api/types'
import { flagReasons, signalRows, type SignalVerdict } from '../lib/signals'

const VERDICT_LABEL: Record<SignalVerdict, string> = {
  pass: 'pass',
  fail: 'concern',
  unavailable: 'not available',
  info: 'context',
}

const VERDICT_CLASS: Record<SignalVerdict, string> = {
  pass: 'bg-emerald-50 text-emerald-800 border-emerald-200',
  fail: 'bg-amber-50 text-amber-900 border-amber-300',
  unavailable: 'bg-slate-50 text-slate-500 border-slate-200',
  info: 'bg-slate-50 text-slate-600 border-slate-200',
}

interface Props {
  signals: FieldSignals
  family: FieldFamily
  name: FieldName
}

export function SignalBreakdown({ signals, family, name }: Props) {
  const rows = signalRows(signals, family, name)
  const reasons = flagReasons(rows)

  return (
    <section aria-labelledby="signal-breakdown-heading" className="mt-4">
      <h3 id="signal-breakdown-heading" className="text-sm font-semibold text-slate-900">
        Why this field is in the queue
      </h3>
      {reasons.length === 0 ? (
        <p className="mt-1 text-sm text-slate-600">
          No single signal failed. The confidence model still put this below the auto accept
          threshold for its family.
        </p>
      ) : (
        <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-800">
          {reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}

      <table className="mt-3 w-full text-left text-sm">
        <caption className="sr-only">Signal breakdown behind the confidence</caption>
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th scope="col" className="py-1 font-medium">
              Signal
            </th>
            <th scope="col" className="py-1 font-medium">
              Value
            </th>
            <th scope="col" className="py-1 font-medium">
              Verdict
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-slate-100 align-top">
              <th scope="row" className="py-1.5 pr-3 font-normal text-slate-700">
                {row.label}
                {/* The wire name, so an engineer reading over the reviewer's
                    shoulder can still map the row back to FieldSignals. */}
                <span className="block font-mono text-xs text-slate-400">{row.code}</span>
              </th>
              <td className="py-1.5 pr-3 font-mono text-slate-900">{row.display}</td>
              <td className="py-1.5">
                <span
                  className={`inline-block rounded border px-1.5 py-0.5 text-xs ${VERDICT_CLASS[row.verdict]}`}
                >
                  {VERDICT_LABEL[row.verdict]}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
