import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { EXCEPTIONS, SUMMARY, VIN_ITEM } from '../../test/fixtures'
import { installApi } from '../../test/harness'
import { queryKeys, useReviewWrite } from '../queries'

function clientWithCaches(): QueryClient {
  // No gcTime override here: a cache seeded with no observer and gcTime 0 is
  // collected before the mutation can invalidate anything, which would pass the
  // assertion for the wrong reason.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  client.setQueryData(queryKeys.summary, SUMMARY)
  client.setQueryData(queryKeys.exceptions({}), EXCEPTIONS)
  return client
}

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

describe('the caches a review write invalidates', () => {
  it('drops the summary and the exception listing after a correction', async () => {
    // A correction reconciles the document again, so the dollars at risk tile,
    // the open exception count and the listing itself are all stale the moment
    // the write lands. Leaving them cached leaves untrue numbers on screen.
    installApi([
      {
        method: 'POST',
        match: /\/correct$/,
        body: {
          ...VIN_ITEM,
          status: 'human_corrected',
          value: '1G1ZT53826F109149',
          reconciliation: {
            exceptions_removed: 1,
            exceptions_added: 0,
            dollars_at_risk_change_cents: -184_000,
          },
        },
      },
      { match: /\/api\/stats\/summary/, body: SUMMARY },
      { match: /\/api\/review\/queue/, body: { items: [], total: 0 } },
      { match: /\/api\/exceptions/, body: EXCEPTIONS },
    ])
    const client = clientWithCaches()
    const { result } = renderHook(() => useReviewWrite(), { wrapper: wrapperFor(client) })

    result.current.correct.mutate({
      fieldId: VIN_ITEM.field_id,
      value: '1G1ZT53826F109149',
      reviewer: 'R Chaurasia',
      label: 'VIN on line 1 of Meridian floorplan statement, June 2026, document 2',
    })

    await waitFor(() => {
      expect(result.current.correct.isSuccess).toBe(true)
    })
    await waitFor(() => {
      expect(client.getQueryState(queryKeys.summary)?.isInvalidated).toBe(true)
      expect(client.getQueryState(queryKeys.exceptions({}))?.isInvalidated).toBe(true)
    })
  })

  it('carries the reconciliation the contract adds through to the caller', async () => {
    installApi([
      {
        method: 'POST',
        match: /\/correct$/,
        body: {
          ...VIN_ITEM,
          status: 'human_corrected',
          value: '1G1ZT53826F109149',
          reconciliation: {
            exceptions_removed: 2,
            exceptions_added: 1,
            dollars_at_risk_change_cents: -50_000,
          },
        },
      },
      { match: /\/api\/stats\/summary/, body: SUMMARY },
      { match: /\/api\/review\/queue/, body: { items: [], total: 0 } },
      { match: /\/api\/exceptions/, body: EXCEPTIONS },
    ])
    const client = clientWithCaches()
    const { result } = renderHook(() => useReviewWrite(), { wrapper: wrapperFor(client) })

    result.current.correct.mutate({
      fieldId: VIN_ITEM.field_id,
      value: '1G1ZT53826F109149',
      reviewer: 'R Chaurasia',
      label: 'VIN on line 1 of Meridian floorplan statement, June 2026, document 2',
    })

    await waitFor(() => {
      expect(result.current.correct.data?.reconciliation).toEqual({
        exceptions_removed: 2,
        exceptions_added: 1,
        dollars_at_risk_change_cents: -50_000,
      })
    })
  })
})
