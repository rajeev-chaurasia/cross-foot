import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { EXCEPTIONS, SUMMARY } from '../../test/fixtures'
import { installApi, renderWithProviders, type ApiRoute } from '../../test/harness'
import { Exceptions } from '../Exceptions'

function routes(): ApiRoute[] {
  return [
    { match: /\/api\/stats\/summary/, body: SUMMARY },
    { match: /\/api\/exceptions/, body: EXCEPTIONS },
    { method: 'POST', match: /\/resolve$/, body: EXCEPTIONS.items[2] },
  ]
}

function rowFor(label: string): HTMLElement {
  return screen
    .getAllByRole('row')
    .filter((row) => row.textContent?.includes(label))[0] as HTMLElement
}

describe('the exceptions dashboard', () => {
  it('leads with the dollars at risk from the summary', async () => {
    installApi(routes())
    renderWithProviders(<Exceptions />)
    expect(await screen.findByText('$3,670.00 at risk')).toBeTruthy()
    expect(await screen.findByText(/4 open exceptions across 105 documents/)).toBeTruthy()
  })

  it('renders the ranking the API returned, absolute impact first', async () => {
    installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/5 matching/)
    const types = screen
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.querySelectorAll('td')[1]?.textContent)
    expect(types).toEqual([
      'Short pay',
      'Duplicate',
      'Amount mismatch',
      'Missing from ledger',
      'Timing difference',
    ])
  })

  it('formats money from integer cents, sign in front of the dollar', async () => {
    installApi(routes())
    renderWithProviders(<Exceptions />)
    expect(await screen.findByText('$2,500.00')).toBeTruthy()
    expect(screen.getByText('-$450.00')).toBeTruthy()
    expect(screen.getByText('-$600.00')).toBeTruthy()
  })

  it('shows a timing difference at zero impact with its memo amount', async () => {
    installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/5 matching/)
    const row = rowFor('Timing difference')
    expect(within(row).getByText('$0.00')).toBeTruthy()
    expect(within(row).getByText('memo $880.00')).toBeTruthy()
  })

  it('expands a row to the statement line and the ledger entry side by side', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<Exceptions />)
    await screen.findByText(/5 matching/)

    const trigger = within(rowFor('Amount mismatch')).getByRole('button', { name: 'Compare' })
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(trigger)

    expect(await screen.findByRole('heading', { name: 'Statement line' })).toBeTruthy()
    const detail = container.querySelector('#exception-detail-exc-1') as HTMLElement
    expect(detail).toBeTruthy()
    expect(within(detail).getByRole('heading', { name: 'Ledger entry' })).toBeTruthy()
    expect(within(detail).getByText('$1,050.00')).toBeTruthy()
    expect(within(detail).getByText('$1,500.00')).toBeTruthy()
    expect(within(detail).getByText('led-parts_payable-00007')).toBeTruthy()
    expect(within(detail).getByText('statement is 450.00 under the ledger')).toBeTruthy()
  })

  it('collapses the row again and keeps aria-expanded honest', async () => {
    installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/5 matching/)

    fireEvent.click(within(rowFor('Amount mismatch')).getByRole('button', { name: 'Compare' }))
    const open = within(rowFor('Amount mismatch')).getByRole('button', { name: 'Hide' })
    expect(open.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(open)
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Ledger entry' })).toBeNull()
    })
  })

  it('asks the API to narrow by type', async () => {
    const api = installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/5 matching/)

    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'amount_mismatch' } })

    await waitFor(() => {
      expect(
        api.calls.some((call) => call.url === '/api/exceptions?type=amount_mismatch'),
      ).toBe(true)
    })
  })

  it('asks the API for a dollar floor in cents', async () => {
    const api = installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/5 matching/)

    fireEvent.change(screen.getByLabelText('Minimum impact'), { target: { value: '100000' } })

    await waitFor(() => {
      expect(
        api.calls.some((call) => call.url === '/api/exceptions?min_impact_cents=100000'),
      ).toBe(true)
    })
  })

  it('resolves an open exception with the reason the reviewer typed', async () => {
    const api = installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/5 matching/)

    fireEvent.click(within(rowFor('Amount mismatch')).getByRole('button', { name: 'Compare' }))
    const reason = await screen.findByLabelText('Resolution')
    fireEvent.change(reason, { target: { value: 'credited on the next statement' } })
    fireEvent.click(screen.getByRole('button', { name: 'Mark resolved' }))

    await waitFor(() => {
      const call = api.calls.find((entry) => entry.url.endsWith('/resolve'))
      expect(call?.url).toBe('/api/exceptions/exc-1/resolve')
      expect(call?.body).toEqual({ resolution: 'credited on the next statement' })
    })
  })
})
