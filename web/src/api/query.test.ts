import { QueryClient, QueryObserver } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import { SpineApiError } from './client';
import { apiQueryOptions } from './query';

function queryClient() {
  return new QueryClient({
    defaultOptions: { queries: { gcTime: Number.POSITIVE_INFINITY } },
  });
}

describe('API query integration', () => {
  it('test_query_exposes_loading_then_success_outcome', async () => {
    let resolve!: (value: string) => void;
    const load = vi.fn(
      () =>
        new Promise<string>((resolvePromise) => {
          resolve = resolvePromise;
        }),
    );
    const client = queryClient();
    const observer = new QueryObserver(
      client,
      apiQueryOptions({ load, queryKey: ['health'] }),
    );
    const unsubscribe = observer.subscribe(() => undefined);

    expect(observer.getCurrentResult()).toMatchObject({
      fetchStatus: 'fetching',
      isPending: true,
    });
    resolve('ready');
    await vi.waitFor(() => {
      expect(observer.getCurrentResult()).toMatchObject({
        data: 'ready',
        status: 'success',
      });
    });

    unsubscribe();
    client.clear();
  });

  it('test_query_cancellation_aborts_obsolete_request', async () => {
    let requestSignal: AbortSignal | undefined;
    const client = queryClient();
    const query = apiQueryOptions({
      load: (signal) => {
        requestSignal = signal;
        return new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(signal.reason));
        });
      },
      queryKey: ['obsolete'],
    });

    const request = client.fetchQuery(query);
    await client.cancelQueries({ queryKey: ['obsolete'] });

    expect(requestSignal?.aborted).toBe(true);
    await expect(request).rejects.toBeDefined();
    client.clear();
  });

  it('test_query_invalidation_exposes_stale_outcome', async () => {
    const client = queryClient();
    const observer = new QueryObserver(
      client,
      apiQueryOptions({
        load: async () => 'ready',
        queryKey: ['freshness'],
        staleTime: 60_000,
      }),
    );
    const unsubscribe = observer.subscribe(() => undefined);

    await observer.refetch();
    expect(observer.getCurrentResult().isStale).toBe(false);
    await client.invalidateQueries({ queryKey: ['freshness'], refetchType: 'none' });
    expect(observer.getCurrentResult().isStale).toBe(true);
    unsubscribe();
    client.clear();
  });

  it('test_retryable_failure_retries_once_and_succeeds', async () => {
    const load = vi
      .fn<(signal: AbortSignal) => Promise<string>>()
      .mockRejectedValueOnce(apiError('after_delay'))
      .mockResolvedValue('ready');
    const client = queryClient();

    const result = await client.fetchQuery(
      apiQueryOptions({ load, queryKey: ['retryable'], retryDelay: 0 }),
    );

    expect(result).toBe('ready');
    expect(load).toHaveBeenCalledTimes(2);
    client.clear();
  });

  it('test_non_retryable_failure_remains_distinguishable_without_retry', async () => {
    const error = apiError('never');
    const load = vi.fn<(signal: AbortSignal) => Promise<string>>().mockRejectedValue(error);
    const client = queryClient();

    await expect(
      client.fetchQuery(apiQueryOptions({ load, queryKey: ['validation'] })),
    ).rejects.toBe(error);
    expect(load).toHaveBeenCalledTimes(1);
    client.clear();
  });
});

function apiError(retryability: 'never' | 'after_delay') {
  return new SpineApiError({
    code: 'spine.client.api_unavailable',
    kind: 'unavailable',
    retryability,
    safeMessage: 'The API is temporarily unavailable.',
    status: 503,
    traceId: '50000000-0000-4000-8000-000000000001',
  });
}
