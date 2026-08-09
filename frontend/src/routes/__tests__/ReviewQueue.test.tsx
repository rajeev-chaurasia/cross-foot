import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DETAILS, QUEUE, SUMMARY, VIN_ITEM } from '../../test/fixtures'
import { installApi, renderWithProviders, type ApiRoute } from '../../test/harness'
import { ReviewQueue } from '../ReviewQueue'

function fieldIdFrom(url: string): string {
  const match = /\/review\/items\/([^/?]+)/.exec(url)
  return match?.[1] ?? ''
}

function routes(overrides: ApiRoute[] = []): ApiRoute[] {
  return [
    ...overrides,
    { match: /\/api\/stats\/summary/, body: SUMMARY },
    { match: /\/api\/review\/queue/, body: QUEUE },
    {
      match: /\/api\/review\/items\/[^/]+$/,
      body: (url: string) => DETAILS[fieldIdFrom(url)],
    },
    {
      method: 'POST',
      match: /\/accept$/,
      body: (url: string) => ({ ...VIN_ITEM, field_id: fieldIdFrom(url), status: 'human_accepted' }),
    },
    {
      method: 'POST',
      match: /\/correct$/,
      body: (url: string, init: RequestInit | undefined) => ({
        ...VIN_ITEM,
        field_id: fieldIdFrom(url),
        status: 'human_corrected',
        value: JSON.parse(String(init?.body)).value as string,
      }),
    },
  ]
}

describe('the review queue', () => {
  it('leads with the share of fields a human has to look at', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    expect(await screen.findByText('Reviewing 19.6% of fields')).toBeTruthy()
    expect(
      await screen.findByText(/490 of 2,500 extracted fields need a human/),
    ).toBeTruthy()
  })

  it('opens on the least trusted field beside the pixels it came from', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    const crop = await screen.findByAltText(/Source crop for VIN on line 1 of document doc-a/)
    expect(crop.getAttribute('src')).toBe('/api/crops/doc-a/fld-a-0001.png')
    expect(screen.getByRole('heading', { name: 'VIN', level: 2 })).toBeTruthy()
  })

  it('asks the API for the queue the contract orders, least trusted first', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    expect(
      api.calls.some((call) =>
        call.url.startsWith('/api/review/queue?limit=50&offset=0&status=needs_review'),
      ),
    ).toBe(true)
  })

  it('moves with j and k', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'j' })
    expect(await screen.findByRole('heading', { name: 'Line amount', level: 2 })).toBeTruthy()

    fireEvent.keyDown(window, { key: 'j' })
    expect(await screen.findByRole('heading', { name: 'Claim number', level: 2 })).toBeTruthy()

    fireEvent.keyDown(window, { key: 'k' })
    expect(await screen.findByRole('heading', { name: 'Line amount', level: 2 })).toBeTruthy()
  })

  it('announces its position for a screen reader', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    expect(
      await screen.findByText(/Field 1 of 3\. VIN on line 1 of document doc-a\./),
    ).toBeTruthy()

    fireEvent.keyDown(window, { key: 'j' })
    expect(await screen.findByText(/Field 2 of 3\. Line amount on line 1/)).toBeTruthy()
  })

  it('accepts with a and advances to the next field', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'a' })

    await waitFor(() => {
      expect(
        api.calls.some(
          (call) =>
            call.method === 'POST' && call.url === '/api/review/items/fld-a-0001/accept',
        ),
      ).toBe(true)
    })
    expect(await screen.findByRole('heading', { name: 'Line amount', level: 2 })).toBeTruthy()
  })

  it('focuses the correction input on c and saves it on Enter', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    expect(document.activeElement).toBe(input)

    fireEvent.change(input, { target: { value: '1G1ZT53826F109149' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      const correction = api.calls.find((call) => call.url.endsWith('/correct'))
      expect(correction?.url).toBe('/api/review/items/fld-a-0001/correct')
      expect(correction?.body).toEqual({ value: '1G1ZT53826F109149', reviewer: 'reviewer' })
    })
    expect(await screen.findByRole('heading', { name: 'Line amount', level: 2 })).toBeTruthy()
  })

  it('leaves j and k alone while the reviewer is typing a correction', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.keyDown(input, { key: 'j' })

    expect(screen.getByRole('heading', { name: 'VIN', level: 2 })).toBeTruthy()
  })

  it('toggles the shortcuts overlay with the question mark', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: '?' })
    const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(dialog.textContent).toContain('Accept the extracted value')

    fireEvent.keyDown(window, { key: '?' })
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).toBeNull()
    })
  })

  it('shows the shortcuts inline so a viewer never has to open the overlay', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    expect(screen.getAllByText('Next field').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Previous field').length).toBeGreaterThan(0)
  })

  it('explains why the field was flagged instead of showing a bare score', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    expect(
      await screen.findByText(
        'VIN check digit failed, so at least one character was read wrong.',
      ),
    ).toBeTruthy()
    expect(screen.getByText('Family validator')).toBeTruthy()
    expect(screen.getAllByText('0.20').length).toBeGreaterThan(0)
  })

  it('rolls back and says why when the API refuses an accept', async () => {
    installApi(
      routes([
        {
          method: 'POST',
          match: /\/accept$/,
          status: 409,
          body: { detail: 'the field was already corrected' },
        },
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'a' })

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('the field was already corrected')

    fireEvent.keyDown(window, { key: 'k' })
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    expect(screen.getAllByText('Needs review').length).toBeGreaterThan(0)
  })

  it('selects a field when it is clicked, and marks it as current', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.click(screen.getByRole('button', { name: /3\. Claim number/ }))

    expect(await screen.findByRole('heading', { name: 'Claim number', level: 2 })).toBeTruthy()
    const selected = screen.getByRole('button', { name: /3\. Claim number/ })
    expect(selected.getAttribute('aria-current')).toBe('true')
  })
})
