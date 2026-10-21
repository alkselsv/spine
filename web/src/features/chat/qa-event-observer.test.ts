import { describe, expect, it, vi } from 'vitest';

import type { QaApiClient } from '../../api/client';
import { observeQAMessageEvents } from './qa-event-observer';

const scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sessionId = '30000000-0000-4000-8000-000000000001';
const messageId = '40000000-0000-4000-8000-000000000001';
const runId = '50000000-0000-4000-8000-000000000001';

describe('production Q&A event observer', () => {
  it('resumes persisted SSE replay with Last-Event-ID after a disconnect', async () => {
    const get = vi.fn()
      .mockResolvedValueOnce({ data: frame('run_started', '60000000-0000-4000-8000-000000000001', 1) })
      .mockRejectedValueOnce(new TypeError('network disconnected'))
      .mockResolvedValueOnce({ data: frame('completed', '60000000-0000-4000-8000-000000000002', 2) });
    const reconnects: number[] = [];
    const observed: string[] = [];

    await observeQAMessageEvents({
      client: { GET: get } as unknown as QaApiClient,
      maxReconnectAttempts: 2,
      messageId,
      onEvent: (event) => observed.push(event.type),
      onReconnect: (attempt) => reconnects.push(attempt),
      pollIntervalMs: 0,
      runId,
      scope,
      sessionId,
      signal: new AbortController().signal,
    });

    expect(observed).toEqual(['run_started', 'completed']);
    expect(reconnects).toEqual([1]);
    expect(get).toHaveBeenCalledTimes(3);
    expect(get.mock.calls.map((call) => call[1].params.header['Last-Event-ID'])).toEqual([
      undefined,
      '60000000-0000-4000-8000-000000000001',
      '60000000-0000-4000-8000-000000000001',
    ]);
  });

  it('fails closed without reconnecting when persisted SSE is malformed', async () => {
    const get = vi.fn().mockResolvedValue({ data: 'event: completed\ndata: not-json\n\n' });
    const reconnects: number[] = [];

    await expect(observeQAMessageEvents({
      client: { GET: get } as unknown as QaApiClient,
      maxReconnectAttempts: 2,
      messageId,
      onEvent: () => undefined,
      onReconnect: (attempt) => reconnects.push(attempt),
      pollIntervalMs: 0,
      runId,
      scope,
      sessionId,
      signal: new AbortController().signal,
    })).rejects.toMatchObject({ name: 'ChatProtocolError' });

    expect(get).toHaveBeenCalledTimes(1);
    expect(reconnects).toEqual([]);
  });
});

function frame(type: 'run_started' | 'completed', eventId: string, sequence: number): string {
  const payload = type === 'run_started' ? { stage: 'admitted' } : { outcome: 'answered' };
  return [
    `id: ${eventId}`,
    `event: ${type}`,
    `data: ${JSON.stringify({
      schema_version: 'qa-event:v1',
      protocol_version: 1,
      event_id: eventId,
      sequence,
      session_id: sessionId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      message_id: messageId,
      run_id: runId,
      trace_id: '70000000-0000-4000-8000-000000000001',
      correlation_id: null,
      causation_id: null,
      type,
      payload,
    })}`,
    '',
    '',
  ].join('\n');
}
