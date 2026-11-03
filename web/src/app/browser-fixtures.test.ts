import { describe, expect, it, vi } from 'vitest';

import { createBrowserQualificationFetch } from './browser-fixtures';

const workspaceId = '10000000-0000-4000-8000-000000000001';
const environmentId = '20000000-0000-4000-8000-000000000001';

describe('browser qualification connection commands', () => {
  it('test_repeated_idempotency_key_returns_stable_first_result_with_one_effect', async () => {
    const fallback = vi.fn<typeof fetch>();
    const fixtureFetch = createBrowserQualificationFetch(fallback);
    const body = {
      approval_id: '30000000-0000-4000-8000-000000000001',
      connection_id: '40000000-0000-4000-8000-000000000001',
      expected_version: 0,
      expected_predecessor_version_id: null,
    };

    const first = await fixtureFetch(commandRequest(body));
    const replay = await fixtureFetch(commandRequest(body));
    const firstPayload = await first.json();
    const replayPayload = await replay.json();

    expect(firstPayload.replay).toBe(false);
    expect(replayPayload.replay).toBe(true);
    expect(replayPayload.command_id).toBe(firstPayload.command_id);
    expect(replayPayload.result).toEqual(firstPayload.result);
    expect(fallback).not.toHaveBeenCalled();
  });

  it('test_reused_idempotency_key_with_changed_command_is_rejected_safely', async () => {
    const fixtureFetch = createBrowserQualificationFetch(vi.fn<typeof fetch>());
    await fixtureFetch(commandRequest({ approval_id: 'approval-a', connection_id: 'connection-a' }));

    const conflict = await fixtureFetch(commandRequest({ approval_id: 'approval-b', connection_id: 'connection-a' }));
    const payload = await conflict.json();

    expect(conflict.status).toBe(409);
    expect(payload.code).toBe('spine.persistence.idempotency_conflict');
    expect(JSON.stringify(payload)).not.toContain('approval-b');
  });
});

function commandRequest(body: Record<string, unknown>): Request {
  return new Request(`${window.location.origin}/api/v1/source-connections`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': 'stable-command-key',
      'X-Workspace-ID': workspaceId,
      'X-Environment-ID': environmentId,
    },
    body: JSON.stringify(body),
  });
}
