import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { METRICS, METRICS_WITHOUT_MODELS, SUMMARY } from '../../test/fixtures'
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

  it('lists the models the scorecard recorded', async () => {
    installApi(routes())
    renderWithProviders(<Metrics />)
    const header = (await screen.findByText('Published metrics')).closest(
      'section',
    ) as HTMLElement
    expect(within(header).getByText(/Models: gemini-3\.5-flash, qwen2\.5vl:7b\./)).toBeTruthy()
  })

  it('calls an empty model list unrecorded rather than saying none were called', async () => {
    installApi([
      { match: /\/api\/stats\/summary/, body: SUMMARY },
      { match: /\/api\/metrics/, body: METRICS_WITHOUT_MODELS },
    ])
    renderWithProviders(<Metrics />)
    const header = (await screen.findByText('Published metrics')).closest(
      'section',
    ) as HTMLElement
    expect(within(header).getByText(/did not record the models it called/)).toBeTruthy()
    // Absence of a record is not absence of a model, and the page may not say it is.
    expect(header.textContent).not.toContain('No model was called')
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

  it('marks the applied point and what the held out split reached at it', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<Metrics />)
    await screen.findByText(/Threshold sweep, calibration split against test split/)
    // One pair per family present in the published points: amount and reference.
    expect(container.querySelectorAll('[data-testid="operating-point"]')).toHaveLength(2)
    expect(container.querySelectorAll('[data-testid="achieved-point"]')).toHaveLength(2)
    expect(container.querySelectorAll('[data-testid="generalization-gap"]')).toHaveLength(2)
  })

  it('prints the reference numbers the test split delivered, not the calibration ones', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<Metrics />)
    await screen.findByText(/Threshold sweep, calibration split against test split/)
    const caption = [...container.querySelectorAll('figcaption')].find((node) =>
      node.textContent?.startsWith('Reference fields'),
    )
    expect(caption).toBeTruthy()
    const text = caption?.textContent ?? ''
    // Both numbers appear, and each says which split produced it.
    expect(text).toContain('Chosen on the calibration split, where it read 100.00%')
    expect(text).toContain('77.57% review')
    expect(text).toContain('On the held out test split the same threshold delivered 95.05%')
    expect(text).toContain('81.96% review')
  })

  it('prints the amount test result rather than the best point on the curve', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<Metrics />)
    await screen.findByText(/Threshold sweep, calibration split against test split/)
    const caption = [...container.querySelectorAll('figcaption')].find((node) =>
      node.textContent?.startsWith('Amount fields'),
    )
    const text = caption?.textContent ?? ''
    expect(text).toContain('On the held out test split the same threshold delivered 95.97%')
    // 100.00% is on the curve at threshold 0.9, which was never applied.
    expect(text).not.toContain('100.00%')
    expect(text).toContain('threshold 0.7000')
  })

  it('names the split beside every sweep number a screen reader hears', async () => {
    installApi(routes())
    renderWithProviders(<Metrics />)
    const table = await screen.findByRole('table', {
      name: 'Threshold sweep for Amount fields',
    })
    const rows = within(table).getAllByRole('row')
    const last = rows[rows.length - 1]
    expect(within(last).getByText('Test')).toBeTruthy()
    expect(within(last).getByText('95.97%')).toBeTruthy()
    expect(within(last).getByText('5.34%')).toBeTruthy()
    // Every other body row is a calibration measurement and says so.
    expect(within(table).getAllByText('Calibration')).toHaveLength(4)
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
