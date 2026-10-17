import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { submitQaFeedback } from './feedback-api';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const answerId = '80000000-0000-4000-8000-000000000001';
const messageId = '50000000-0000-4000-8000-000000000001';
const runId = '70000000-0000-4000-8000-000000000001';
const feedbackId = '90000000-0000-4000-8000-000000000001';

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

afterEach(() => vi.restoreAllMocks());

describe('Q&A feedback adapter', () => {
  it('test_answer_feedback_uses_canonical_contract_and_returns_safe_receipt', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      expect(request.headers.get('Idempotency-Key')).toBe('feedback-key-1');
      expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
      expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
      expect(await request.json()).toEqual({
        target_kind: 'answer',
        target_id: answerId,
        outcome: 'answer.helpful',
        run_id: runId,
        message_id: messageId,
        comment: 'Синтетический отзыв',
      });
      return response({
        schema_version: 1,
        trace_id: '60000000-0000-4000-8000-000000000001',
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        result: {
          schema_version: 1,
          status: 'recorded',
          feedback_id: feedbackId,
          feedback_version: 1,
          target_kind: 'answer',
          target_id: answerId,
          run_id: runId,
          message_id: messageId,
          citation_id: null,
          outcome: 'answer.helpful',
          audit_event_id: null,
        },
      });
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });

    const result = await submitQaFeedback(client, {
      request: {
        target_kind: 'answer',
        target_id: answerId,
        outcome: 'answer.helpful',
        run_id: runId,
        message_id: messageId,
        comment: 'Синтетический отзыв',
      },
      idempotencyKey: 'feedback-key-1',
      signal: new AbortController().signal,
    });

    expect(result.feedback_id).toBe(feedbackId);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('test_citation_feedback_targets_exact_citation_identity', async () => {
    const citationId = 'c0000000-0000-4000-8000-000000000001';
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      expect(await request.json()).toEqual({
        target_kind: 'citation',
        target_id: citationId,
        citation_id: citationId,
        outcome: 'citation.incorrect',
        run_id: runId,
        message_id: messageId,
      });
      return response({
        schema_version: 1,
        trace_id: '60000000-0000-4000-8000-000000000001',
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        result: {
          schema_version: 1,
          status: 'recorded',
          feedback_id: feedbackId,
          feedback_version: 1,
          target_kind: 'citation',
          target_id: citationId,
          run_id: runId,
          message_id: messageId,
          citation_id: citationId,
          outcome: 'citation.incorrect',
          audit_event_id: null,
        },
      });
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });

    await submitQaFeedback(client, {
      request: {
        target_kind: 'citation',
        target_id: citationId,
        citation_id: citationId,
        outcome: 'citation.incorrect',
        run_id: runId,
        message_id: messageId,
      },
      idempotencyKey: 'feedback-key-2',
      signal: new AbortController().signal,
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('test_feedback_response_with_mismatched_identity_fails_closed', async () => {
    const fetcher = vi.fn<typeof fetch>(async () => response({
      schema_version: 1,
      trace_id: '60000000-0000-4000-8000-000000000001',
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      result: {
        schema_version: 1,
        status: 'recorded',
        feedback_id: feedbackId,
        feedback_version: 1,
        target_kind: 'answer',
        target_id: '80000000-0000-4000-8000-000000000099',
        run_id: runId,
        message_id: messageId,
        citation_id: null,
        outcome: 'answer.helpful',
        audit_event_id: null,
      },
    }));
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });

    await expect(submitQaFeedback(client, {
      request: {
        target_kind: 'answer',
        target_id: answerId,
        outcome: 'answer.helpful',
        run_id: runId,
        message_id: messageId,
      },
      idempotencyKey: 'feedback-key-3',
      signal: new AbortController().signal,
    })).rejects.toThrow('Feedback is unavailable.');
  });
});
