/**
 * Test harness: a fake API and a provider wrapper.
 *
 * Every test drives the UI through a stubbed `fetch` that answers with the
 * shapes tests/contract/ pins, so nothing here touches a socket and nothing
 * here can drift from the contract without a fixture edit.
 */

import type { ReactElement } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { render, type RenderResult } from '@testing-library/react'
import { vi, type Mock } from 'vitest'

export interface ApiRoute {
  method?: 'GET' | 'POST'
  /** Matched against the request path, query string included. */
  match: RegExp
  status?: number
  body: unknown | ((url: string, init: RequestInit | undefined) => unknown)
}

export interface FakeApi {
  fetch: Mock
  calls: { url: string; method: string; body: unknown }[]
}

/** Install a fetch that serves the given routes and records every call. */
export function installApi(routes: ApiRoute[]): FakeApi {
  const calls: { url: string; method: string; body: unknown }[] = []

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    const parsed =
      typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined
    calls.push({ url, method, body: parsed })

    const route = routes.find(
      (candidate) => (candidate.method ?? 'GET') === method && candidate.match.test(url),
    )
    if (route === undefined) {
      throw new Error(`No fake route for ${method} ${url}`)
    }
    const status = route.status ?? 200
    const body = typeof route.body === 'function' ? route.body(url, init) : route.body
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: '',
      json: async () => body,
    } as Response
  })

  vi.stubGlobal('fetch', fetchMock)
  return { fetch: fetchMock, calls }
}

export function renderWithProviders(ui: ReactElement, route = '/'): RenderResult {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}
