import { describe, expect, it } from 'vitest';

import {
  CHAT_PROTOCOL_VERSION,
  ChatProtocolError,
  decodeChatEvent,
  type ChatEvent,
} from './protocol';

const ids = {
  sessionId: 'session-1',
  messageId: 'message-1',
  runId: 'run-1',
  traceId: 'trace-1',
  workspaceId: 'workspace-1',
  environmentId: 'environment-1',
};

function event<T extends ChatEvent['type']>(
  type: T,
  payload: ChatEvent<T>['payload'],
  sequence = 1,
): ChatEvent<T> {
  return {
    protocolVersion: CHAT_PROTOCOL_VERSION,
    eventId: `event-${sequence}`,
    sequence,
    ...ids,
    type,
    payload,
  } as ChatEvent<T>;
}

describe('Spine chat event protocol', () => {
  it.each([
    ['run_started', { stage: 'accepted' }],
    ['stage_changed', { stage: 'grounding' }],
    ['answer_delta', {
      delta: 'Разрешённый фрагмент',
      disclosure: { receiptId: 'disclosure-1', status: 'authorized' },
    }],
    ['citation_added', {
      citation: {
        citationId: 'citation-1',
        claimId: 'claim-1',
        sourceRevisionId: 'revision-1',
        locator: { kind: 'page', value: '4' },
      },
      disclosure: { receiptId: 'disclosure-1', status: 'authorized' },
    }],
    ['clarification_required', { question: 'Уточните период.' }],
    ['abstained', { reason: 'insufficient_evidence' }],
    ['denied', { reason: 'authorization_denied' }],
    ['completed', { outcome: 'answered' }],
    ['failed', { reason: 'internal_failure' }],
    ['cancelled', { reason: 'request_cancelled' }],
    ['heartbeat', {}],
  ] as const)('decodes the versioned %s event', (type, payload) => {
    expect(decodeChatEvent(event(type, payload))).toMatchObject({ type, payload });
  });

  it('test_protocol_rejects_version_mismatch_and_malformed_payload', () => {
    expect(() =>
      decodeChatEvent({
        ...event('heartbeat', {}),
        protocolVersion: 2,
      }),
    ).toThrow(ChatProtocolError);
    expect(() =>
      decodeChatEvent({ ...event('stage_changed', {} as never), payload: { stage: '' } }),
    ).toThrow(ChatProtocolError);
  });

  it('test_protocol_rejects_protected_content_without_disclosure_authorization', () => {
    expect(() =>
      decodeChatEvent(
        event('answer_delta', {
          delta: 'candidate answer',
          disclosure: { receiptId: 'disclosure-1', status: 'pending' },
        } as never),
      ),
    ).toThrow(ChatProtocolError);
  });

  it('test_protocol_rejects_candidate_output_and_provider_details', () => {
    expect(() =>
      decodeChatEvent({
        ...event('stage_changed', { stage: 'grounding' }),
        payload: { stage: 'grounding', candidate_output: 'do not cross the seam' },
      }),
    ).toThrow(ChatProtocolError);
    expect(() =>
      decodeChatEvent({
        ...event('failed', { reason: 'internal_failure' }),
        payload: { reason: 'internal_failure', provider_error: 'secret' },
      }),
    ).toThrow(ChatProtocolError);
  });
});
