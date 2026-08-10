/**
 * Typed fetch client for the routes docs/contracts-phase3.md freezes.
 *
 * One function per route, no route strings anywhere else in the app. Crop URLs
 * are never assembled here: the API hands back `crop_url` on every item and the
 * UI uses that string verbatim, so path containment stays a server concern.
 */

import type {
  CorrectionRequest,
  CorrectionResponse,
  ExceptionListParams,
  ExceptionListResponse,
  ExceptionRecord,
  MetricsResponse,
  ResolutionRequest,
  ReviewItem,
  ReviewItemDetail,
  ReviewQueueParams,
  ReviewQueueResponse,
  StatsSummary,
} from './types'

export const API_BASE = '/api'

/** A non 2xx response, carrying whatever explanation the API sent with it. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

type QueryValue = string | number | undefined

function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') {
      continue
    }
    search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded === '' ? '' : `?${encoded}`
}

async function errorFrom(response: Response): Promise<ApiError> {
  const fallback = `${response.status} ${response.statusText}`.trim()
  let message = fallback
  try {
    const body: unknown = await response.json()
    if (body !== null && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      message = typeof detail === 'string' ? detail : JSON.stringify(detail)
    }
  } catch {
    // A body that is not JSON tells us nothing more than the status line does.
  }
  return new ApiError(response.status, message)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: init?.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    throw await errorFrom(response)
  }
  return (await response.json()) as T
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function getSummary(): Promise<StatsSummary> {
  return request<StatsSummary>('/stats/summary')
}

export function getReviewQueue(params: ReviewQueueParams = {}): Promise<ReviewQueueResponse> {
  return request<ReviewQueueResponse>(`/review/queue${queryString({ ...params })}`)
}

export function getReviewItem(fieldId: string): Promise<ReviewItemDetail> {
  return request<ReviewItemDetail>(`/review/items/${encodeURIComponent(fieldId)}`)
}

export function acceptReviewItem(fieldId: string): Promise<ReviewItem> {
  return post<ReviewItem>(`/review/items/${encodeURIComponent(fieldId)}/accept`)
}

export function correctReviewItem(
  fieldId: string,
  correction: CorrectionRequest,
): Promise<CorrectionResponse> {
  return post<CorrectionResponse>(
    `/review/items/${encodeURIComponent(fieldId)}/correct`,
    correction,
  )
}

export function getExceptions(params: ExceptionListParams = {}): Promise<ExceptionListResponse> {
  return request<ExceptionListResponse>(`/exceptions${queryString({ ...params })}`)
}

export function resolveException(
  exceptionId: string,
  resolution: ResolutionRequest,
): Promise<ExceptionRecord> {
  return post<ExceptionRecord>(
    `/exceptions/${encodeURIComponent(exceptionId)}/resolve`,
    resolution,
  )
}

export function getMetrics(): Promise<MetricsResponse> {
  return request<MetricsResponse>('/metrics')
}
