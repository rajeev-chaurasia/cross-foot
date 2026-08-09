import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { METRICS, SUMMARY } from '../../test/fixtures'
import { installApi, renderWithProviders, type ApiRoute } from '../../test/harness'
import { Metrics } from '../Metrics'

function routes(): ApiRoute[] {
  return [
    { match: /\/api\/stats\/summary/, body: SUMMARY },
    { match: /\/api\/metrics/, body: METRICS },
  ]
}

describe('the metrics page', () => {
  it('names the scorecard every figure came from', async () => {
    installApi(routes())
    renderWithProviders(<Metrics />)
    const header = (await screen.findByText('Published metrics')).closest(
      'section',
    ) as HTMLElement
    expect(within(header).getByText('20260807T090000-bbbbbbb')).toBeTruthy()
    expect(within(header).getByText(/Test split, seed 42/)).toBeTruthy()
  })

  it('reports cost per document in list price microusd', async () => {
    installApi(routes())
    renderWithProviders(<Metrics />)
    expect(await screen.findByText('$0.045')).toBeTruthy()
    expect(screen.getByText(/free tier run still shows what the work costs/)).toBeTruthy()
  })

  it('prints per field accuracy as the counts the scorecard published', async () => {
    installApi(routes())
    renderWithProviders(<Metrics />)
    expect(await screen.findByText(/121 of 150/)).toBeTruthy()
    expect(screen.getByText(/154 of 220/)).toBeTruthy()
    expect(screen.getByText('140 extracted')).toBeTruthy()
  })

  it('draws one calibration dot per published bin against the ideal diagonal', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<Metrics />)
    await screen.findByText('Reliability diagram')
    expect(container.querySelectorAll('[data-testid="calibration-point"]')).toHaveLength(2)
    expect(container.querySelectorAll('[data-testid="ideal-diagonal"]')).toHaveLength(1)
  })

  it('marks the operating point on the sweep and says which rule chose it', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<Metrics />)
    await screen.findByText('Threshold sweep')
    // One sweep per family present in the published points: amount and reference.
    expect(container.querySelectorAll('[data-testid="operating-point"]')).toHaveLength(2)
    expect(container.textContent).toContain('Operating point at threshold')
    expect(container.textContent).toContain(
      'the published point with the highest auto accept precision',
    )
  })

  it('repeats the sweep numbers in a table for a screen reader', async () => {
    installApi(routes())
    renderWithProviders(<Metrics />)
    const table = await screen.findByRole('table', {
      name: 'Threshold sweep for Amount fields',
    })
    expect(within(table).getByText('99.64%')).toBeTruthy()
    expect(within(table).getByText('18.1%')).toBeTruthy()
  })

  it('says so plainly when the API could not be reached', async () => {
    installApi([
      { match: /\/api\/stats\/summary/, body: SUMMARY },
      { match: /\/api\/metrics/, status: 503, body: { detail: 'no scorecard committed yet' } },
    ])
    renderWithProviders(<Metrics />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('no scorecard committed yet')
  })
})
