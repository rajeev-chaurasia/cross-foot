import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest'

import { App } from '../../App'
import {
  ACCEPTED_DETAIL,
  ACCEPTED_ITEM,
  ACCEPTED_QUEUE,
  AMOUNT_ITEM,
  DETAILS,
  DOC_A,
  EXCEPTIONS,
  LONG_QUEUE_TOTAL,
  QUEUE,
  SUMMARY,
  TABULAR_QUEUE,
  VIN_ITEM,
  longQueuePage,
} from '../../test/fixtures'
import { installApi, renderWithProviders, type ApiRoute } from '../../test/harness'
import { ReviewQueue } from '../ReviewQueue'

function fieldIdFrom(url: string): string {
  const match = /\/review\/items\/([^/?]+)/.exec(url)
  return match?.[1] ?? ''
}

/** The page size ReviewQueue asks the API for. */
const PAGE_SIZE = 50

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

/**
 * A correct route that answers with the reconciliation the case under test wants.
 *
 * Passing nothing omits the field entirely, which is the API build the backend
 * ships mid change; passing null is the contract's "could not be reconciled".
 */
const OMITTED = Symbol('no reconciliation field at all')

function correctRoute(reconciliation: unknown = OMITTED): ApiRoute {
  return {
    method: 'POST',
    match: /\/correct$/,
    body: (url: string, init: RequestInit | undefined) => {
      const saved = {
        ...VIN_ITEM,
        field_id: fieldIdFrom(url),
        status: 'human_corrected',
        value: JSON.parse(String(init?.body)).value as string,
      }
      return reconciliation === OMITTED ? saved : { ...saved, reconciliation }
    },
  }
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
  it('leads with the count of fields a human has to look at', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    expect(await screen.findByText('490 fields waiting for a reviewer')).toBeTruthy()
    expect(await screen.findByText(/19.6% of the 2,500 fields in this database/)).toBeTruthy()
  })

  it('says the share is this database now and not the published review rate', async () => {
    // The two used to read alike by coincidence, so a reader comparing the
    // screen with the README concluded they agreed. Saying which is which is
    // the whole fix.
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    expect(await screen.findByText(/every split included/)).toBeTruthy()
    expect(await screen.findByText(/it falls as reviewing happens/)).toBeTruthy()
    expect(await screen.findByText(/not the published review rate/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'metrics page' }).getAttribute('href')).toBe(
      '/metrics',
    )
  })

  it('prints the share the API divided rather than dividing two counts itself', async () => {
    // 490 over 2500 is 19.6 percent either way, so the fixture sends a share the
    // counts do not produce: anything doing the arithmetic here prints 19.6.
    installApi(
      routes([
        {
          match: /\/api\/stats\/summary/,
          body: { ...SUMMARY, review_queue_share: 0.271 },
        },
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    expect(await screen.findByText(/27.1% of the 2,500 fields in this database/)).toBeTruthy()
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

// D10. A cold clone was told to loosen filters it had never set.
describe('a queue with nothing in it', () => {
  const EMPTY: ApiRoute = { match: /\/api\/review\/queue/, body: { items: [], total: 0 } }

  it('says the database is empty rather than blaming the filters', async () => {
    installApi(
      routes([
        EMPTY,
        {
          match: /\/api\/stats\/summary/,
          body: {
            ...SUMMARY,
            fields_extracted: 0,
            review_queue_depth: 0,
            review_queue_share: 0,
          },
        },
      ]),
    )
    renderWithProviders(<ReviewQueue />)

    expect(
      await screen.findByText(
        'No statements have been read into this database yet, so there is nothing to review.',
      ),
    ).toBeTruthy()
    expect(screen.queryByText('Nothing matches these filters.')).toBeNull()
  })

  it('still blames the filters when the database has fields and none match', async () => {
    installApi(routes([EMPTY]))
    renderWithProviders(<ReviewQueue />)

    expect(await screen.findByText('Nothing matches these filters.')).toBeTruthy()
  })
})

// D1. Focusing the Status select and pressing `a`, which is how a browser jumps
// to the first option starting with A, recorded a human acceptance nobody made.
// The queue depth fell by one with the filter still on needs_review.
describe('keys pressed inside a form control', () => {
  it('accepts nothing when a is pressed inside a filter select', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(screen.getByLabelText('Status'), { key: 'a' })
    fireEvent.keyDown(screen.getByLabelText('Family'), { key: 'a' })
    // The same key on the page itself does accept, so the count below is the
    // guard working rather than the test having pressed nothing at all.
    fireEvent.keyDown(window, { key: 'a' })

    await waitFor(() => {
      expect(api.calls.filter((call) => call.url.endsWith('/accept'))).toHaveLength(1)
    })
  })

  it('leaves the queue and the correction box alone for j, k and c in a select', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    const statusFilter = screen.getByLabelText('Status')
    fireEvent.keyDown(statusFilter, { key: 'j' })
    fireEvent.keyDown(statusFilter, { key: 'k' })
    fireEvent.keyDown(statusFilter, { key: 'c' })

    expect(screen.getByRole('heading', { name: 'VIN', level: 2 })).toBeTruthy()
    expect(document.activeElement).not.toBe(screen.getByLabelText(/Correction/))
  })

  it('still moves and accepts from the page itself', async () => {
    const api = installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'j' })
    await screen.findByRole('heading', { name: 'Line amount', level: 2 })
    fireEvent.keyDown(window, { key: 'a' })

    await waitFor(() => {
      expect(
        api.calls.some((call) => call.url === `/api/review/items/${AMOUNT_ITEM.field_id}/accept`),
      ).toBe(true)
    })
  })
})

// D6. scrollIntoView on mount moved Chrome's sequential focus starting point
// into the queue list, so the first Tab skipped the skip link, the nav and the
// three filter controls.
describe('scrolling the queue list', () => {
  function trackScrolls(): Mock {
    const scrolled = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrolled,
    })
    return scrolled
  }

  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  })

  it('scrolls nothing for the landing selection the page made for itself', async () => {
    const scrolled = trackScrolls()
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    expect(scrolled).not.toHaveBeenCalled()
  })

  it('scrolls nothing when a filter change restarts the queue', async () => {
    const scrolled = trackScrolls()
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.change(screen.getByLabelText('Family'), { target: { value: 'amount' } })
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'VIN', level: 2 })).toBeTruthy()
    })
    expect(scrolled).not.toHaveBeenCalled()
  })

  it('keeps the selected field in view once the reviewer is moving', async () => {
    const scrolled = trackScrolls()
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    fireEvent.keyDown(window, { key: 'j' })
    await screen.findByRole('heading', { name: 'Line amount', level: 2 })
    expect(scrolled).toHaveBeenCalled()
  })
})

// D3. Setting Status to "Auto accepted" is one dropdown from the landing state,
// and every field behind it was headed "Why this field is in the queue".
describe('a field that is not in the queue', () => {
  function acceptedRoutes(): ApiRoute[] {
    return routes([
      { match: /\/api\/review\/queue/, body: ACCEPTED_QUEUE },
      { match: /\/api\/review\/items\/[^/]+$/, body: ACCEPTED_DETAIL },
    ])
  }

  it('does not claim an auto accepted field is in the queue', async () => {
    installApi(acceptedRoutes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'Statement date', level: 2 })

    expect(await screen.findByRole('heading', { name: 'What the signals said' })).toBeTruthy()
    expect(screen.queryByText('Why this field is in the queue')).toBeNull()
  })

  it('does not tell the reviewer the model held back a field it accepted', async () => {
    installApi(acceptedRoutes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'Statement date', level: 2 })

    expect(await screen.findByText('No signal raised a concern about this field.')).toBeTruthy()
    expect(screen.queryByText(/below the auto accept threshold/)).toBeNull()
  })

  it('keeps the queue wording for a field that really is in the queue', async () => {
    installApi(
      routes([
        {
          match: /\/api\/review\/queue/,
          body: { items: [{ ...ACCEPTED_ITEM, status: 'needs_review' }], total: 1 },
        },
        {
          match: /\/api\/review\/items\/[^/]+$/,
          body: { ...ACCEPTED_DETAIL, status: 'needs_review' },
        },
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'Statement date', level: 2 })

    expect(await screen.findByRole('heading', { name: 'Why this field is in the queue' }))
      .toBeTruthy()
    expect(screen.getByText(/below the auto accept threshold for its family/)).toBeTruthy()
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
    expect(screen.getAllByText('Scanned PDF').length).toBeGreaterThan(0)
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

  it('states plainly that a tabular format has no page image', async () => {
    // 973 fields in the built database are CSV fields, and every one of them
    // used to answer with a PDFium data format error, which reads as a corrupt
    // statement. The file is healthy; it simply has no pages.
    installApi(routes([{ match: /\/api\/review\/queue/, body: TABULAR_QUEUE }]))
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'Line amount', level: 2 })
    const crop = screen.getByRole('region', { name: 'Source crop' })

    expect(await within(crop).findByText('This format has no page image')).toBeTruthy()
    expect(within(crop).getByText(/read from rows of data rather than from a printed page/))
      .toBeTruthy()
    expect(within(crop).queryByText(/not available/)).toBeNull()
  })

  it('never asks for a picture of a format that has none', async () => {
    const api = installApi(routes([{ match: /\/api\/review\/queue/, body: TABULAR_QUEUE }]))
    renderWithProviders(<ReviewQueue />)
    const crop = screen.getByRole('region', { name: 'Source crop' })
    await within(crop).findByText('This format has no page image')

    expect(api.calls.some((call) => call.url.includes('/api/crops/'))).toBe(false)
    expect(within(crop).queryByRole('img')).toBeNull()
    expect(within(crop).queryByRole('link', { name: 'Open in a new tab' })).toBeNull()
  })

  it('offers a zoom and a full size link so the pixels can actually be read', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    const crop = screen.getByRole('region', { name: 'Source crop' })
    await within(crop).findByText('The whole page.')

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

    // Reading the live region is how a reviewer knows where they are, so it is
    // the right thing to walk. Every step is awaited, because a step across a
    // page boundary has to fetch before it can announce anything.
    const seen: number[] = []
    for (let step = 1; step <= LONG_QUEUE_TOTAL; step += 1) {
      const announced = await screen.findByText(`Field ${String(step)} of 120.`, {
        exact: false,
      })
      seen.push(Number(/Field (\d+) of 120\./.exec(announced.textContent ?? '')?.[1]))
      fireEvent.keyDown(window, { key: 'j' })
    }

    expect(seen).toHaveLength(LONG_QUEUE_TOTAL)
    expect(new Set(seen).size).toBe(LONG_QUEUE_TOTAL)
    expect(seen[0]).toBe(1)
    expect(seen[LONG_QUEUE_TOTAL - 1]).toBe(LONG_QUEUE_TOTAL)
    // Three pages of fifty, so the walk really did cross two boundaries.
    expect(Math.ceil(LONG_QUEUE_TOTAL / PAGE_SIZE)).toBe(3)
  }, 30_000)

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

// C1. A reviewer corrected a misread amount and nothing told them it mattered.
// Clearing disputed money is the entire product and it was invisible.
describe('what a correction turns out to have been worth', () => {
  async function save(value = '1G1ZT53826F109149'): Promise<void> {
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')
    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value } })
    fireEvent.keyDown(input, { key: 'Enter' })
  }

  it('counts a single cleared exception in the singular', async () => {
    installApi(
      routes([
        correctRoute({
          exceptions_removed: 1,
          exceptions_added: 0,
          dollars_at_risk_change_cents: -184_000,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save()

    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain(
      'Cleared 1 exception. $1,840.00 less at risk on this statement.',
    )
    expect(outcome.textContent).not.toContain('1 exceptions')
  })

  it('counts several cleared exceptions in the plural', async () => {
    installApi(
      routes([
        correctRoute({
          exceptions_removed: 4,
          exceptions_added: 0,
          dollars_at_risk_change_cents: -367_000,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save()

    expect(
      await screen.findByText(
        'Cleared 4 exceptions. $3,670.00 less at risk on this statement.',
      ),
    ).toBeTruthy()
  })

  it('says money left the statement when the change is negative', async () => {
    installApi(
      routes([
        correctRoute({
          exceptions_removed: 2,
          exceptions_added: 0,
          dollars_at_risk_change_cents: -184_000,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save()

    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain('Your correction cleared exceptions')
    expect(outcome.textContent).toContain('$1,840.00 less at risk')
    // The wire value is negative; the minus sign is carried by the words.
    expect(outcome.textContent).not.toContain('-$1,840.00')
  })

  it('treats a positive change as money found rather than damage done', async () => {
    // Uncovering a real discrepancy is the product working, so the sentence
    // must not read as the reviewer having made the statement worse.
    installApi(
      routes([
        correctRoute({
          exceptions_removed: 0,
          exceptions_added: 1,
          dollars_at_risk_change_cents: 184_000,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save()

    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain('Your correction found disputed money')
    expect(outcome.textContent).toContain(
      'Opened 1 exception. $1,840.00 more at risk on this statement, ' +
        'money the earlier reading missed.',
    )
  })

  it('says nothing about reconciliation when the document could not be reconciled', async () => {
    installApi(routes([correctRoute(null)]))
    renderWithProviders(<ReviewQueue />)
    await save()

    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain('Correction saved')
    // Not an error, and not a row of zeros standing in for an answer.
    expect(outcome.textContent).not.toContain('0 exceptions')
    expect(outcome.textContent).not.toContain('$0.00')
    expect(outcome.textContent).not.toContain('at risk')
  })

  it('survives an API build that has no reconciliation field yet', async () => {
    installApi(routes([correctRoute()]))
    renderWithProviders(<ReviewQueue />)
    await save()

    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain('Correction saved')
    expect(outcome.textContent).not.toContain('$0.00')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says plainly when the correction changed nothing', async () => {
    installApi(
      routes([
        correctRoute({
          exceptions_removed: 0,
          exceptions_added: 0,
          dollars_at_risk_change_cents: 0,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save()

    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain('Nothing on this statement changed')
    expect(outcome.textContent).toContain(
      'No exceptions opened or closed, and no change to the money at risk.',
    )
  })

  it('names the field the outcome belongs to, not the one now on screen', async () => {
    installApi(
      routes([
        correctRoute({
          exceptions_removed: 1,
          exceptions_added: 0,
          dollars_at_risk_change_cents: -184_000,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save()

    // Saving advances to the next field, so the panel has to say which field it
    // is reporting on or it reads as a claim about the one in view.
    await screen.findByRole('heading', { name: 'Line amount', level: 2 })
    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain(
      'VIN on line 1 of Meridian floorplan statement, June 2026, document 2',
    )
    expect(outcome.textContent).toContain('1G1ZT53826F109149')
  })

  it('claims no outcome at all when the API refuses the correction', async () => {
    installApi(
      routes([
        {
          method: 'POST',
          match: /\/correct$/,
          status: 422,
          body: { detail: 'value is not parseable for family reference' },
        },
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save('not a vin')

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('value is not parseable for family reference')
    // The live region stays mounted, and stays empty. It is the claim that is
    // absent, not the container.
    expect(screen.getByRole('status').textContent).toBe('')
    expect(screen.queryByText('Correction saved')).toBeNull()
  })

  it('stays on screen while the reviewer keeps moving through the queue', async () => {
    // A toast is gone before someone working with j and a can read it.
    installApi(
      routes([
        correctRoute({
          exceptions_removed: 1,
          exceptions_added: 0,
          dollars_at_risk_change_cents: -184_000,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save()
    await screen.findByText(/Cleared 1 exception\./)

    fireEvent.keyDown(window, { key: 'j' })
    await screen.findByRole('heading', { name: 'Claim number', level: 2 })
    fireEvent.keyDown(window, { key: 'k' })
    await screen.findByRole('heading', { name: 'Line amount', level: 2 })

    expect(screen.getByText(/Cleared 1 exception\./)).toBeTruthy()
  })
})

// D4. The panel echoed the draft, so "$1,840" was reported as saved while the
// database held 1840.00 and "7/4/2026" while it held 2026-07-04.
describe('the value the panel says was saved', () => {
  /** A correct route that stores the canonical form of whatever was typed. */
  function canonicalRoute(stored: string): ApiRoute {
    return {
      method: 'POST',
      match: /\/correct$/,
      body: (url: string) => ({
        ...VIN_ITEM,
        field_id: fieldIdFrom(url),
        status: 'human_corrected',
        value: stored,
      }),
    }
  }

  async function save(value: string): Promise<void> {
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')
    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value } })
    fireEvent.keyDown(input, { key: 'Enter' })
  }

  it('reports the amount the API stored, not the amount that was typed', async () => {
    installApi(routes([canonicalRoute('1840.00')]))
    renderWithProviders(<ReviewQueue />)
    await save('$1,840')

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toContain('saved as')
    })
    const outcome = screen.getByRole('status')
    expect(outcome.textContent).toContain('1840.00')
    expect(outcome.textContent).not.toContain('$1,840')
  })

  it('reports the date the API stored, not the date that was typed', async () => {
    installApi(routes([canonicalRoute('2026-07-04')]))
    renderWithProviders(<ReviewQueue />)
    await save('7/4/2026')

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toContain('saved as')
    })
    const outcome = screen.getByRole('status')
    expect(outcome.textContent).toContain('2026-07-04')
    expect(outcome.textContent).not.toContain('7/4/2026')
  })

  it('shows the typed text only while the write is still open', async () => {
    let release = (): void => {}
    const held = new Promise<void>((resolve) => {
      release = () => {
        resolve()
      }
    })
    installApi(
      routes([
        {
          method: 'POST',
          match: /\/correct$/,
          body: (url: string) =>
            held.then(() => ({
              ...VIN_ITEM,
              field_id: fieldIdFrom(url),
              status: 'human_corrected',
              value: '2026-07-04',
            })),
        },
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save('7/4/2026')

    const outcome = await screen.findByRole('status')
    await waitFor(() => {
      expect(outcome.textContent).toContain('7/4/2026')
    })
    // Nothing is stored yet, so the panel does not yet say anything was.
    expect(outcome.textContent).toContain('saving as')
    expect(outcome.textContent).not.toContain('saved as')

    release()
    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toContain('2026-07-04')
    })
    expect(screen.getByRole('status').textContent).not.toContain('7/4/2026')
  })
})

// D5. The success panel goes to trouble to say which field it is about. A
// refusal said nothing, and by the time it arrived the reviewer had moved on.
describe('a correction the API refuses', () => {
  async function save(value: string): Promise<void> {
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')
    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value } })
    fireEvent.keyDown(input, { key: 'Enter' })
  }

  function refusingRoutes(detail: unknown): ApiRoute[] {
    return routes([{ method: 'POST', match: /\/correct$/, status: 422, body: { detail } }])
  }

  it('names the field the refusal is about, not the one now on screen', async () => {
    installApi(refusingRoutes('value is not parseable for family reference'))
    renderWithProviders(<ReviewQueue />)
    await save('not a vin')

    await screen.findByRole('heading', { name: 'Line amount', level: 2 })
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain(
      'VIN on line 1 of Meridian floorplan statement, June 2026, document 2 was not saved.',
    )
    expect(alert.textContent).toContain('value is not parseable for family reference')
  })

  it('reads as a sentence when the framework refused rather than a route', async () => {
    installApi(
      refusingRoutes([
        {
          type: 'string_too_long',
          loc: ['body', 'value'],
          msg: 'String should have at most 256 characters',
          input: 'K'.repeat(300),
        },
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await save('K'.repeat(300))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain(
      'The server would not accept that. Check the value and try again.',
    )
    expect(alert.textContent).not.toContain('string_too_long')
    expect(alert.textContent).not.toContain('KKKK')
  })

  it('wraps a long refusal rather than pushing the card past the viewport', async () => {
    installApi(refusingRoutes(`value is not parseable: ${'K'.repeat(300)}`))
    renderWithProviders(<ReviewQueue />)
    await save('K'.repeat(300))

    const alert = await screen.findByRole('alert')
    expect(alert.className).toContain('break-words')
  })
})

// D9. A reviewer checks the dashboard against the number the panel just claimed
// and comes back. The claim used to be gone.
describe('the outcome panel across a page change', () => {
  it('is still there after a trip to the exceptions dashboard and back', async () => {
    installApi([
      { match: /\/api\/exceptions/, body: EXCEPTIONS },
      ...routes([
        correctRoute({
          exceptions_removed: 1,
          exceptions_added: 0,
          dollars_at_risk_change_cents: -184_000,
        }),
      ]),
    ])
    renderWithProviders(<App />)

    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')
    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value: '1G1ZT53826F109149' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await screen.findByText(/Cleared 1 exception\./)

    fireEvent.click(screen.getByRole('link', { name: 'Exceptions' }))
    await screen.findByText(/ranked by how/)

    fireEvent.click(screen.getByRole('link', { name: 'Review queue' }))
    expect(await screen.findByText(/Cleared 1 exception\./)).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain(
      'VIN on line 1 of Meridian floorplan statement, June 2026, document 2',
    )
  })
})

// D10. A live region inserted into the page the moment it has something to say
// is missed by some screen readers.
describe('the live region around the outcome', () => {
  it('is mounted and empty before any correction is saved', async () => {
    installApi(routes())
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })

    const region = screen.getByRole('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
    expect(region.textContent).toBe('')
  })
})

describe('a correction that takes its time', () => {
  /** A correct route that does not answer until the test lets it. */
  function heldCorrectRoute(): { route: ApiRoute; release: () => void } {
    let release = (): void => {}
    const held = new Promise<void>((resolve) => {
      release = () => {
        resolve()
      }
    })
    return {
      release: () => {
        release()
      },
      route: {
        method: 'POST',
        match: /\/correct$/,
        body: (url: string, init: RequestInit | undefined) =>
          held.then(() => ({
            ...VIN_ITEM,
            field_id: fieldIdFrom(url),
            status: 'human_corrected',
            value: JSON.parse(String(init?.body)).value as string,
            reconciliation: {
              exceptions_removed: 1,
              exceptions_added: 0,
              dollars_at_risk_change_cents: -184_000,
            },
          })),
      },
    }
  }

  it('advances the queue without waiting for the server to reconcile', async () => {
    const held = heldCorrectRoute()
    installApi(routes([held.route]))
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')

    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value: '1G1ZT53826F109149' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // The round trip is still open and the reviewer is already on the next field.
    expect(await screen.findByRole('heading', { name: 'Line amount', level: 2 })).toBeTruthy()

    held.release()
    expect(await screen.findByText(/Cleared 1 exception\./)).toBeTruthy()
  })

  it('says it is still checking rather than showing nothing at all', async () => {
    const held = heldCorrectRoute()
    installApi(routes([held.route]))
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')

    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value: '1G1ZT53826F109149' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    const outcome = await screen.findByRole('status')
    expect(outcome.textContent).toContain('Saving your correction')
    expect(outcome.textContent).toContain('Checking what it changed on this statement.')

    held.release()
    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toContain('Cleared 1 exception.')
    })
  })
})

// C4. Dollars at risk and the open exception count both move on a correction,
// and both are read from the summary the tiles print.
describe('the numbers a correction makes stale', () => {
  it('refetches the summary after a correction lands', async () => {
    const api = installApi(
      routes([
        correctRoute({
          exceptions_removed: 1,
          exceptions_added: 0,
          dollars_at_risk_change_cents: -184_000,
        }),
      ]),
    )
    renderWithProviders(<ReviewQueue />)
    await screen.findByRole('heading', { name: 'VIN', level: 2 })
    await typeReviewer('R Chaurasia')

    const before = api.calls.filter((call) => call.url.startsWith('/api/stats/summary')).length

    fireEvent.keyDown(window, { key: 'c' })
    const input = screen.getByLabelText(/Correction/)
    fireEvent.change(input, { target: { value: '1G1ZT53826F109149' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await screen.findByText(/Cleared 1 exception\./)

    await waitFor(() => {
      const after = api.calls.filter((call) => call.url.startsWith('/api/stats/summary')).length
      expect(after).toBeGreaterThan(before)
    })
  })
})
