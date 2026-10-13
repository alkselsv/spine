import {
  queryOptions,
  type QueryKey,
  type UseQueryOptions,
} from '@tanstack/react-query';

import { SpineApiError } from './client';

export interface ApiQueryDefinition<TData> {
  load: (signal: AbortSignal) => Promise<TData>;
  queryKey: QueryKey;
  retryDelay?: number;
  staleTime?: number;
}

export function apiQueryOptions<TData>({
  load,
  queryKey,
  retryDelay = 1_000,
  staleTime = 30_000,
}: ApiQueryDefinition<TData>): UseQueryOptions<TData, SpineApiError> {
  return queryOptions({
    queryFn: ({ signal }) => load(signal),
    queryKey,
    retry: (failureCount, error) =>
      error instanceof SpineApiError &&
      error.retryability === 'after_delay' &&
      failureCount < 1,
    retryDelay,
    staleTime,
  });
}
