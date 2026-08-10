import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DOC_A, EXCEPTIONS, longExceptionPage, LONG_EXCEPTIONS_TOTAL, SUMMARY } from '../../test/fixtures'
import { installApi, renderWithProviders, type ApiRoute } from '../../test/harness'
import { Exceptions } from '../Exceptions'

function routes(): ApiRoute[] {
  return [
    { match: /\/api\/stats\/summary/, body: SUMMARY },
    { match: /\/api\/exceptions/, body: EXCEPTIONS },
    { method: 'POST', match: /\/resolve$/, body: EXCEPTIONS.items[2] },
  ]
}

/** 751 exceptions, the count the real run opens, served fifty at a time. */
function pagedRoutes(): ApiRoute[] {
  return [
    { match: /\/api\/stats\/summary/, body: SUMMARY },
    {
      match: /\/api\/exceptions/,
      body: (url: string) => {
        const query = new URLSearchParams(url.slice(url.indexOf('?')))
        return longExceptionPage(Number(query.get('offset') ?? 0), Number(query.get('limit') ?? 50))
      },
    },
  ]
}

function dataRows(): HTMLElement[] {
  return screen.getAllByRole('row').slice(1)
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
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)
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
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)
    const row = rowFor('Timing difference')
    expect(within(row).getByText('$0.00')).toBeTruthy()
    expect(within(row).getByText('memo $880.00')).toBeTruthy()
  })

  it('expands a row to the statement line and the ledger entry side by side', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)

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
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)

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
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)

    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'amount_mismatch' } })

    await waitFor(() => {
      expect(
        api.calls.some((call) => call.url === '/api/exceptions?type=amount_mismatch&limit=50&offset=0'),
      ).toBe(true)
    })
  })

  it('asks the API for a dollar floor in cents', async () => {
    const api = installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)

    fireEvent.change(screen.getByLabelText('Minimum impact'), { target: { value: '100000' } })

    await waitFor(() => {
      expect(
        api.calls.some((call) => call.url === '/api/exceptions?min_impact_cents=100000&limit=50&offset=0'),
      ).toBe(true)
    })
  })

  it('resolves an open exception with the reason the reviewer typed', async () => {
    const api = installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)

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

// A2. The document column used to be a primary key.
describe('naming the document on the dashboard', () => {
  it('names the statement in words, with the key underneath it', async () => {
    installApi(routes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 5 of 5 exceptions/)
    const row = rowFor('Short pay')
    expect(
      within(row).getByText(/Meridian floorplan statement, June 2026, document 2, line 3/),
    ).toBeTruthy()
    expect(within(row).getByText(DOC_A)).toBeTruthy()
  })
})

// B2. 751 rows in one 38,735 pixel table, and the only buttons were "Compare".
describe('paging the dashboard', () => {
  it('asks the API for one page rather than the whole listing', async () => {
    const api = installApi(pagedRoutes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 50 of 751 exceptions/)
    expect(api.calls.some((call) => call.url === '/api/exceptions?limit=50&offset=0')).toBe(true)
    expect(dataRows()).toHaveLength(50)
  })

  it('walks to the next page and keeps the rank counting from the whole listing', async () => {
    installApi(pagedRoutes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 50 of 751 exceptions/)
    expect(dataRows()[0].querySelector('td')?.textContent).toBe('1')

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText(/Showing 51 to 100 of 751 exceptions/)
    expect(dataRows()[0].querySelector('td')?.textContent).toBe('51')
    expect(screen.getByText('Page 2 of 16')).toBeTruthy()
  })

  it('walks every page without skipping or repeating an exception', async () => {
    installApi(pagedRoutes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 50 of 751 exceptions/)

    const seen = new Set<string>()
    for (let page = 0; ; page += 1) {
      const first = page * 50 + 1
      const last = Math.min(first + 49, LONG_EXCEPTIONS_TOTAL)
      await screen.findByText(
        new RegExp(`Showing ${String(first)} to ${String(last)} of 751 exceptions`),
      )
      for (const row of dataRows()) {
        seen.add(row.querySelector('td')?.textContent ?? '')
      }
      const next = screen.getByRole('button', { name: 'Next page' })
      if (next.hasAttribute('disabled')) {
        break
      }
      fireEvent.click(next)
    }

    expect(seen.size).toBe(LONG_EXCEPTIONS_TOTAL)
    expect(seen.has('1')).toBe(true)
    expect(seen.has('751')).toBe(true)
    // Sixteen pages of fifty, so the walk really did turn every one of them.
    expect(Math.ceil(LONG_EXCEPTIONS_TOTAL / 50)).toBe(16)
  }, 30_000)

  it('goes back to the first page when the filter narrows', async () => {
    const api = installApi(pagedRoutes())
    renderWithProviders(<Exceptions />)
    await screen.findByText(/Showing 1 to 50 of 751 exceptions/)

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText(/Showing 51 to 100 of 751 exceptions/)

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'open' } })
    await waitFor(() => {
      expect(
        api.calls.some((call) => call.url === '/api/exceptions?status=open&limit=50&offset=0'),
      ).toBe(true)
    })
  })
})
