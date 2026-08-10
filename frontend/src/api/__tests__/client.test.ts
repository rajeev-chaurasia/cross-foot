import { describe, expect, it } from 'vitest'

import { QUEUE, SUMMARY } from '../../test/fixtures'
import { installApi } from '../../test/harness'
import {
  ApiError,
  acceptReviewItem,
  correctReviewItem,
  getReviewQueue,
  getSummary,
} from '../client'

describe('the typed client', () => {
  it('calls the frozen summary route', async () => {
    const api = installApi([{ match: /\/api\/stats\/summary$/, body: SUMMARY }])
    await expect(getSummary()).resolves.toEqual(SUMMARY)
    expect(api.calls[0]?.url).toBe('/api/stats/summary')
  })

  it('drops unset filters from the queue query string', async () => {
    const api = installApi([{ match: /\/api\/review\/queue/, body: QUEUE }])
    await getReviewQueue({ limit: 50, offset: 0, status: 'needs_review' })
    expect(api.calls[0]?.url).toBe('/api/review/queue?limit=50&offset=0&status=needs_review')
  })

  it('sends no query string when nothing is filtered', async () => {
    const api = installApi([{ match: /\/api\/review\/queue/, body: QUEUE }])
    await getReviewQueue()
    expect(api.calls[0]?.url).toBe('/api/review/queue')
  })

  it('posts an accept with no body', async () => {
    const api = installApi([
      { method: 'POST', match: /\/accept$/, body: QUEUE.items[0] },
    ])
    await acceptReviewItem('fld-a-0001')
    expect(api.calls[0]?.url).toBe('/api/review/items/fld-a-0001/accept')
    expect(api.calls[0]?.method).toBe('POST')
  })

  it('posts a correction as value and reviewer', async () => {
    const api = installApi([
      { method: 'POST', match: /\/correct$/, body: QUEUE.items[0] },
    ])
    await correctReviewItem('fld-a-0002', { value: '1999.99', reviewer: 'rc' })
    expect(api.calls[0]?.body).toEqual({ value: '1999.99', reviewer: 'rc' })
  })

  it('raises the API explanation when a correction is refused', async () => {
    installApi([
      {
        method: 'POST',
        match: /\/correct$/,
        status: 422,
        body: { detail: 'value is not parseable for family amount' },
      },
    ])
    await expect(
      correctReviewItem('fld-a-0002', { value: 'not a number', reviewer: 'rc' }),
    ).rejects.toThrow(/family amount/)
  })

  it('carries the status code on the error it raises', async () => {
    installApi([{ match: /\/api\/stats\/summary$/, status: 500, body: { detail: 'boom' } }])
    const error = await getSummary().catch((raised: unknown) => raised)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(500)
  })
})

// A 300 character correction put the whole pydantic error object on the screen,
// echoed input and all, and the unbroken token pushed the card past the viewport.
describe('an error the framework wrote rather than a person', () => {
  const OVERLONG = 'K'.repeat(300)

  function validationRoute(): void {
    installApi([
      {
        method: 'POST',
        match: /\/correct$/,
        status: 422,
        body: {
          detail: [
            {
              type: 'string_too_long',
              loc: ['body', 'value'],
              msg: 'String should have at most 256 characters',
              input: OVERLONG,
            },
          ],
        },
      },
    ])
  }

  async function refusal(): Promise<ApiError> {
    return (await correctReviewItem('fld-a-0002', {
      value: OVERLONG,
      reviewer: 'rc',
    }).catch((raised: unknown) => raised)) as ApiError
  }

  it('answers a validation list with a sentence', async () => {
    validationRoute()
    expect((await refusal()).message).toBe(
      'The server would not accept that. Check the value and try again.',
    )
  })

  it('puts none of the wire object in front of the reviewer', async () => {
    validationRoute()
    const message = (await refusal()).message
    expect(message).not.toContain('string_too_long')
    expect(message).not.toContain('loc')
    expect(message).not.toContain(OVERLONG)
    expect(message).not.toContain('[{')
  })

  it('still shows the explanation when a route wrote one itself', async () => {
    installApi([
      {
        method: 'POST',
        match: /\/correct$/,
        status: 422,
        body: { detail: 'value is not parseable for family amount' },
      },
    ])
    expect((await refusal()).message).toBe('value is not parseable for family amount')
  })
})
