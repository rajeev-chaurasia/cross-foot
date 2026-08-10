import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  AMOUNT_ITEM,
  DETAILS,
  DOC_A,
  LONG_QUEUE_TOTAL,
  QUEUE,
  SUMMARY,
  VIN_ITEM,
  longQueuePage,
} from '../../test/fixtures'
import { installApi, renderWithProviders, type ApiRoute } from '../../test/harness'
import { ReviewQueue } from '../ReviewQueue'

function fieldIdFrom(url: string): string {
  const match = /\/review\/items\/([^/?]+)/.exec(url)
  return match?.[1] ?? ''
}

function pagingFrom(url: string): { limit: number; offset: number } {
  const query = new URLSearchParams(url.slice(url.indexOf('?')))
  return { limit: Number(query.get('limit') ?? 50), offset: Number(query.get('offset') ?? 0) }
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

/** A queue of 120 fields, served a page at a time from whatever the UI asked for. */
function pagedRoutes(): ApiRoute[] {
  return [
    { match: /\/api\/stats\/summary/, body: SUMMARY },
    {
      match: /\/api\/review\/queue/,
      body: (url: string) => {
        const { limit, offset } = pagingFrom(url)
        return longQueuePage(offset, limit)
      },
    },
    {
      match: /\/api\/review\/items\/[^/]+$/,
      body: (url: string) => ({ ...DETAILS[VIN_ITEM.field_id], field_id: fieldIdFrom(url) }),
    },
  ]
}

async function typeReviewer(name: string): Promise<void> {
  fireEvent.change(await screen.findByLabelText('Reviewer'), { target: { value: name } })
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
    const crop = await screen.findByAltText(
      /Source crop for VIN on line 1 of Meridian floorplan statement, June 2026, document 2/,
    )
    expect(crop.getAttribute('src')).toBe(VIN_ITEM.crop_url)
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
      await screen.findByText(
        /Field 1 of 3\. VIN on line 1 of Meridian floorplan statement, June 2026, document 2\./,
      ),
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
            call.method === 'POST' &&
            call.url === `/api/review/items/${VIN_ITEM.field_id}/accept`,
        ),
      ).toBe(true)
    })
    expect(await screen.findByRole('heading', { name: 'Line amount', level: 2 })).toBeTruthy()
  })

  it('focuses the correction input on c and saves it on Enter', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')

    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    expect(document.activeElement).toBe(input)

    fireEvent.change(input, { target: { value: '1G1ZT53826F109149' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      const correction = api.calls.find((call) => call.url.endsWith('/correct'))
      expect(correction?.url).toBe(`/api/review/items/${VIN_ITEM.field_id}/correct`)
      expect(correction?.body).toEqual({
        value: '1G1ZT53826F109149',
        reviewer: 'R Chaurasia',
      })
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
    expect(screen.getByText('Passes the format check')).toBeTruthy()
    expect(screen.getAllByText('20.0%').length).toBeGreaterThan(0)
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

// A1. The tier is dataset generator metadata. Showing it implies the system
// knows something no real statement carries, which is the whole reason the
// confidence model was stripped of it.
describe('what the review surface refuses to show', () => {
  it('shows no quality tier anywhere, as a value or as a filter', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    const text = container.textContent ?? ''
    expect(text).not.toContain('Quality tier')
    expect(text).not.toContain('Scan heavy')
    expect(text).not.toContain('Scan light')
    expect(text).not.toContain('Clean digital')
    expect(screen.queryByLabelText('Quality tier')).toBeNull()
  })

  it('never sends a tier filter to the API', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    expect(api.calls.some((call) => call.url.includes('tier='))).toBe(false)
  })

  it('keeps the route, which the router reads off the file itself', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    expect(await screen.findByText('Route')).toBeTruthy()
    expect(screen.getAllByText('Scanned pdf').length).toBeGreaterThan(0)
  })
})

// A2. A reviewer needs the statement and the line, not our primary keys.
describe('naming the field and the document', () => {
  it('leads with the statement in words, not with the doc id', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    const crop = screen.getByRole('region', { name: 'Source crop' })
    expect(
      within(crop).getByText('Meridian floorplan statement, June 2026, document 2'),
    ).toBeTruthy()
  })

  it('does not use the raw field id as the field label', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    const heading = await screen.findByRole('heading', { name: 'VIN', level: 2 })
    const panel = heading.closest('section') as HTMLElement
    // The heading and the sentence under it name the field; the id is a caption.
    expect(within(panel).getByText(/Reference field on line 1 of Meridian floorplan/)).toBeTruthy()
    const raw = within(panel).getByText(VIN_ITEM.field_id)
    expect(raw.className).toContain('font-mono')
    expect(raw.tagName).toBe('P')
  })

  it('keeps the raw ids reachable for a bug report', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    expect(screen.getAllByText(DOC_A).length).toBeGreaterThan(0)
    expect(screen.getByText(VIN_ITEM.field_id)).toBeTruthy()
  })
})

// A3. Confidence used to print as a bare 0.25 beside three panels of percentages.
describe('the numbers on the queue', () => {
  it('prints confidence as a percentage, rounded, never as the raw float', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'j' })
    await screen.findByRole('heading', { name: 'Line amount', level: 2 })
    expect(screen.getAllByText('25.0%').length).toBeGreaterThan(0)
    expect(screen.queryByText(String(AMOUNT_ITEM.confidence))).toBeNull()
  })
})

// A4. The table under the plain sentence should read in the same register.
describe('the signal table', () => {
  it('names each signal in words and keeps the wire name as a caption', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    expect(screen.getByText('Passes the format check')).toBeTruthy()
    expect(screen.getByText('The two readings agree')).toBeTruthy()
    expect(screen.getByText('How this file was read')).toBeTruthy()
    // The internal names are still on screen, as captions an engineer can map.
    expect(screen.getByText('validator_pass')).toBeTruthy()
    expect(screen.getByText('det_llm_agreement')).toBeTruthy()
    expect(screen.getByText('route')).toBeTruthy()
  })

  it('drops the internal jargon that used to be the label', async () => {
    installApi(routes())
    const { container } = renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    const labels = [...container.querySelectorAll('tbody th')].map((cell) =>
      cell.firstChild?.textContent,
    )
    expect(labels).not.toContain('Family validator')
    expect(labels).not.toContain('Crossfoot residual')
    expect(labels).not.toContain('Read by')
    expect(labels).not.toContain('Self consistency')
  })
})

// B1. The crop is the whole premise of the screen.
describe('the source crop', () => {
  it('says plainly when the image is a whole page fallback', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    const crop = screen.getByRole('region', { name: 'Source crop' })
    expect(await within(crop).findByText('The whole page.')).toBeTruthy()
    expect(within(crop).getByText(/No position was recorded for this value/)).toBeTruthy()
  })

  it('says when the image is the value itself rather than a fallback', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'j' })
    await screen.findByRole('heading', { name: 'Line amount', level: 2 })
    const crop = screen.getByRole('region', { name: 'Source crop' })
    expect(await within(crop).findByText('The value itself.')).toBeTruthy()
  })

  it('offers a zoom and a full size link so the pixels can actually be read', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    const crop = screen.getByRole('region', { name: 'Source crop' })

    const zoom = within(crop).getByRole('button', { name: 'Zoom to full size' })
    expect(zoom.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(zoom)
    expect(
      within(crop).getByRole('button', { name: 'Fit to the panel' }).getAttribute('aria-pressed'),
    ).toBe('true')

    const link = within(crop).getByRole('link', { name: 'Open in a new tab' })
    expect(link.getAttribute('href')).toBe(VIN_ITEM.crop_url)
  })
})

// B3. 50 of 1,901 with no way to reach the rest was the finding.
describe('paging the queue', () => {
  it('offers a control that reaches past the first page', async () => {
    installApi(pagedRoutes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByText(/Showing 1 to 50 of 120 fields/)

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText(/Showing 51 to 100 of 120 fields/)

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText(/Showing 101 to 120 of 120 fields/)
    expect(screen.getByRole('button', { name: 'Next page' }).hasAttribute('disabled')).toBe(true)
  })

  it('walks the whole queue with j, skipping nothing and repeating nothing', async () => {
    installApi(pagedRoutes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    const seen: number[] = []
    for (let step = 0; step < LONG_QUEUE_TOTAL; step += 1) {
      const announcement = await screen.findByText(/Field \d+ of 120\./)
      const place = /Field (\d+) of 120\./.exec(announcement.textContent ?? '')
      seen.push(Number(place?.[1]))
      fireEvent.keyDown(window, { key: 'j' })
      // A page boundary needs the next page to arrive before the next read.
      if (seen.length % 50 === 0) {
        await screen.findByText(`Field ${String(seen.length + 1)} of 120.`, { exact: false })
      }
    }

    expect(seen).toHaveLength(LONG_QUEUE_TOTAL)
    expect(new Set(seen).size).toBe(LONG_QUEUE_TOTAL)
    expect(seen[0]).toBe(1)
    expect(seen[LONG_QUEUE_TOTAL - 1]).toBe(LONG_QUEUE_TOTAL)
  })

  it('loads the next page when j runs off the end of this one', async () => {
    const api = installApi(pagedRoutes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByText(/Showing 1 to 50 of 120 fields/)

    // Jump to the last row of page one, then step off it.
    fireEvent.click(screen.getByRole('button', { name: /^50\. VIN/ }))
    await screen.findByText('Field 50 of 120.', { exact: false })
    fireEvent.keyDown(window, { key: 'j' })

    await screen.findByText('Field 51 of 120.', { exact: false })
    expect(
      api.calls.some((call) => call.url.includes('/api/review/queue?limit=50&offset=50')),
    ).toBe(true)
  })

  it('lands on the last field of the page before when k runs off the top', async () => {
    installApi(pagedRoutes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByText(/Showing 1 to 50 of 120 fields/)

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText('Field 51 of 120.', { exact: false })

    fireEvent.keyDown(window, { key: 'k' })
    await screen.findByText('Field 50 of 120.', { exact: false })
  })

  it('shows how far through the queue the reviewer is', async () => {
    installApi(pagedRoutes())
    renderWithProviders(<ReviewQueue />)
    const bar = await screen.findByRole('progressbar', {
      name: 'Position in the review queue',
    })
    expect(bar.getAttribute('aria-valuenow')).toBe('1')
    expect(bar.getAttribute('aria-valuemax')).toBe('120')

    fireEvent.keyDown(window, { key: 'j' })
    await waitFor(() => {
      expect(bar.getAttribute('aria-valuenow')).toBe('2')
    })
  })
})

// B4. The box used to be pre-filled with the literal word "reviewer".
describe('who a correction is attributed to', () => {
  it('starts the reviewer box empty, with a placeholder rather than a value', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    const box = (await screen.findByLabelText('Reviewer')) as HTMLInputElement
    expect(box.value).toBe('')
    expect(box.placeholder).toBe('Your name')
  })

  it('refuses to save a correction until a name is given', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    const save = screen.getByRole('button', { name: 'Save correction' })
    expect(save.hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(/Put your name in the Reviewer box/)).toBeTruthy()

    fireEvent.keyDown(window, { key: 'c' })
    fireEvent.change(screen.getByLabelText(/Correction/), { target: { value: 'XYZ' } })
    fireEvent.keyDown(screen.getByLabelText(/Correction/), { key: 'Enter' })
    expect(api.calls.some((call) => call.url.endsWith('/correct'))).toBe(false)

    await typeReviewer('R Chaurasia')
    expect(
      screen.getByRole('button', { name: 'Save correction' }).hasAttribute('disabled'),
    ).toBe(false)
  })

  it('never attributes a correction to the word reviewer', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('  Dana Okafor  ')

    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value: '1G1ZT53826F109149' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      const correction = api.calls.find((call) => call.url.endsWith('/correct'))
      expect((correction?.body as { reviewer: string } | undefined)?.reviewer).toBe('Dana Okafor')
    })
  })
})
