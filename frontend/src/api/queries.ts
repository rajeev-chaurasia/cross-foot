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
  useMutationState,
  useQuery,
  useQueryClient,
  type MutationState,
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
  CorrectionReconciliation,
  CorrectionRequest,
  CorrectionResponse,
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
  /** Not a query. It is how the last correction is found again after a remount. */
  correction: ['review', 'correct'] as const,
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

/**
 * What a correction is fired with.
 *
 * `label` is the field in words. It rides along with the mutation rather than
 * sitting in component state because the reviewer has moved on by the time the
 * answer arrives, and may have left the page entirely. `correctReviewItem`
 * never sees it, so nothing extra crosses the wire.
 */
export interface CorrectionVariables extends CorrectionRequest {
  fieldId: string
  label: string
}

/** The last correction fired this session, whatever page is on screen now. */
export interface LastCorrection {
  status: 'pending' | 'success' | 'error'
  /** The field it was written against, in words. */
  label: string
  /** What the reviewer typed, which is not always what gets stored. */
  typed: string
  /** The canonical value the API stored. Null until the answer arrives. */
  saved: string | null
  reconciliation: CorrectionReconciliation | null | undefined
  error: Error | null
}

/**
 * The outcome panel's data, read from the mutation itself.
 *
 * The mutation cache lives on the QueryClient, above the router, so a reviewer
 * who walks to the dashboard to check the number this panel just claimed finds
 * the claim still there when they come back. Nothing here tracks the mutation's
 * state a second time, which is what would let the panel and the write disagree.
 */
export function useLastCorrection(): LastCorrection | null {
  const states = useMutationState({
    filters: { mutationKey: queryKeys.correction },
    select: (mutation) =>
      mutation.state as MutationState<CorrectionResponse, Error, CorrectionVariables>,
  })
  const last = states.at(-1)
  if (last === undefined || last.status === 'idle' || last.variables === undefined) {
    return null
  }
  return {
    status: last.status,
    label: last.variables.label,
    typed: last.variables.value,
    saved: last.data?.value ?? null,
    reconciliation: last.data?.reconciliation,
    error: last.error,
  }
}

function patchItem(item: ReviewItem, patch: Partial<ReviewItem>): ReviewItem {
  return { ...item, ...patch }
}

export function useReviewWrite(): {
  accept: UseMutationResult<ReviewItem, Error, string>
  correct: UseMutationResult<CorrectionResponse, Error, CorrectionVariables>
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

  const correct = useMutation<CorrectionResponse, Error, CorrectionVariables, ReviewSnapshot>({
    // Keyed and never collected, so `useLastCorrection` can still find this
    // write after the reviewer has navigated away from the queue and back.
    mutationKey: queryKeys.correction,
    gcTime: Infinity,
    mutationFn: ({ fieldId, value, reviewer }) => correctReviewItem(fieldId, { value, reviewer }),
    onMutate: ({ fieldId, value }) =>
      snapshotAndPatch(fieldId, { status: 'human_corrected', value }),
    onError: (_error, _variables, snapshot) => {
      rollback(snapshot)
    },
    onSettled: (_data, _error, { fieldId }) => {
      settle(fieldId)
      // A correction reconciles the document again, so the exceptions listing
      // and the dollars at risk beside it are both stale. `settle` has already
      // dropped the summary, which is where both tiles read their counts.
      void client.invalidateQueries({ queryKey: queryKeys.exceptionsRoot })
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
