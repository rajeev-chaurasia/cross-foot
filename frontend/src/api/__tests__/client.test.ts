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
