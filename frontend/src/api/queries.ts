/**
 * TanStack Query bindings for the frozen routes.
 *
 * Accept and correct both patch the cached queue and the cached detail before
 * the request leaves, and both put the snapshot back if the API refuses, so a
 * reviewer holding down `a` never waits on a round trip and never sees a lie
 * that survives an error.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import {
  acceptReviewItem,
  correctReviewItem,
  getExceptions,
  getMetrics,
  getReviewItem,
  getReviewQueue,
  getSummary,
  resolveException,
} from './client'
import type {
  CorrectionRequest,
  ExceptionListParams,
  ExceptionListResponse,
  ExceptionRecord,
  MetricsResponse,
  ReviewItem,
  ReviewItemDetail,
  ReviewQueueParams,
  ReviewQueueResponse,
  StatsSummary,
} from './types'

export const queryKeys = {
  summary: ['stats', 'summary'] as const,
  reviewQueueRoot: ['review', 'queue'] as const,
  reviewQueue: (params: ReviewQueueParams) => ['review', 'queue', params] as const,
  reviewItemRoot: ['review', 'item'] as const,
  reviewItem: (fieldId: string) => ['review', 'item', fieldId] as const,
  exceptionsRoot: ['exceptions'] as const,
  exceptions: (params: ExceptionListParams) => ['exceptions', params] as const,
  metrics: ['metrics'] as const,
}

export function useSummary(): UseQueryResult<StatsSummary, Error> {
  return useQuery({ queryKey: queryKeys.summary, queryFn: getSummary })
}

export function useReviewQueue(
  params: ReviewQueueParams,
): UseQueryResult<ReviewQueueResponse, Error> {
  return useQuery({
    queryKey: queryKeys.reviewQueue(params),
    queryFn: () => getReviewQueue(params),
  })
}

export function useReviewItem(
  fieldId: string | null,
): UseQueryResult<ReviewItemDetail, Error> {
  return useQuery({
    queryKey: queryKeys.reviewItem(fieldId ?? ''),
    queryFn: () => getReviewItem(fieldId as string),
    enabled: fieldId !== null,
  })
}

export function useMetrics(): UseQueryResult<MetricsResponse, Error> {
  return useQuery({ queryKey: queryKeys.metrics, queryFn: getMetrics })
}

export function useExceptions(
  params: ExceptionListParams,
): UseQueryResult<ExceptionListResponse, Error> {
  return useQuery({
    queryKey: queryKeys.exceptions(params),
    queryFn: () => getExceptions(params),
  })
}

/** Cache entries touched by an optimistic review write, kept for rollback. */
interface ReviewSnapshot {
  queues: [readonly unknown[], ReviewQueueResponse | undefined][]
  details: [readonly unknown[], ReviewItemDetail | undefined][]
}

function patchItem(item: ReviewItem, patch: Partial<ReviewItem>): ReviewItem {
  return { ...item, ...patch }
}

export function useReviewWrite(): {
  accept: UseMutationResult<ReviewItem, Error, string>
  correct: UseMutationResult<ReviewItem, Error, { fieldId: string } & CorrectionRequest>
} {
  const client = useQueryClient()

  const snapshotAndPatch = async (
    fieldId: string,
    patch: Partial<ReviewItem>,
  ): Promise<ReviewSnapshot> => {
    await client.cancelQueries({ queryKey: queryKeys.reviewQueueRoot })
    await client.cancelQueries({ queryKey: queryKeys.reviewItem(fieldId) })

    const queues = client.getQueriesData<ReviewQueueResponse>({
      queryKey: queryKeys.reviewQueueRoot,
    })
    const details = client.getQueriesData<ReviewItemDetail>({
      queryKey: queryKeys.reviewItem(fieldId),
    })

    client.setQueriesData<ReviewQueueResponse>(
      { queryKey: queryKeys.reviewQueueRoot },
      (current) =>
        current === undefined
          ? current
          : {
              ...current,
              items: current.items.map((item) =>
                item.field_id === fieldId ? patchItem(item, patch) : item,
              ),
            },
    )
    client.setQueryData<ReviewItemDetail>(queryKeys.reviewItem(fieldId), (current) =>
      current === undefined ? current : { ...current, ...patch },
    )

    return { queues, details }
  }

  const rollback = (snapshot: ReviewSnapshot | undefined): void => {
    if (snapshot === undefined) {
      return
    }
    for (const [key, value] of snapshot.queues) {
      client.setQueryData(key, value)
    }
    for (const [key, value] of snapshot.details) {
      client.setQueryData(key, value)
    }
  }

  const settle = (fieldId: string): void => {
    void client.invalidateQueries({ queryKey: queryKeys.reviewQueueRoot })
    void client.invalidateQueries({ queryKey: queryKeys.reviewItem(fieldId) })
    void client.invalidateQueries({ queryKey: queryKeys.summary })
  }

  const accept = useMutation<ReviewItem, Error, string, ReviewSnapshot>({
    mutationFn: (fieldId: string) => acceptReviewItem(fieldId),
    onMutate: (fieldId) => snapshotAndPatch(fieldId, { status: 'human_accepted' }),
    onError: (_error, _fieldId, snapshot) => {
      rollback(snapshot)
    },
    onSettled: (_data, _error, fieldId) => {
      settle(fieldId)
    },
  })

  const correct = useMutation<
    ReviewItem,
    Error,
    { fieldId: string } & CorrectionRequest,
    ReviewSnapshot
  >({
    mutationFn: ({ fieldId, value, reviewer }) => correctReviewItem(fieldId, { value, reviewer }),
    onMutate: ({ fieldId, value }) =>
      snapshotAndPatch(fieldId, { status: 'human_corrected', value }),
    onError: (_error, _variables, snapshot) => {
      rollback(snapshot)
    },
    onSettled: (_data, _error, { fieldId }) => {
      settle(fieldId)
    },
  })

  return { accept, correct }
}

export function useResolveException(): UseMutationResult<
  ExceptionRecord,
  Error,
  { exceptionId: string; resolution: string }
> {
  const client = useQueryClient()
  return useMutation<ExceptionRecord, Error, { exceptionId: string; resolution: string }>({
    mutationFn: ({ exceptionId, resolution }) => resolveException(exceptionId, { resolution }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: queryKeys.exceptionsRoot })
      void client.invalidateQueries({ queryKey: queryKeys.summary })
    },
  })
}
