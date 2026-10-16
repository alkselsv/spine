import { describe, expect, it } from 'vitest';

import {
  CHAT_PROTOCOL_VERSION,
  ChatProtocolError,
  decodeChatEvent,
  decodeChatEventStream,
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

  it('test_qa_event_v1_wire_envelope_maps_to_the_normalized_chat_contract', () => {
    const decoded = decodeChatEvent(wireEvent('answer_delta', {
      delta: 'Разрешённый фрагмент',
      disclosure: {
        accepted_output_id: '70000000-0000-4000-8000-000000000001',
        receipt_id: '80000000-0000-4000-8000-000000000001',
        schema_version: 'qa-disclosure-receipt:v1',
        status: 'authorized',
        validation_reference: '90000000-0000-4000-8000-000000000001',
      },
    }));

    expect(decoded).toEqual({
      protocolVersion: 1,
      schemaVersion: 'qa-event:v1',
      eventId: '10000000-0000-4000-8000-000000000001',
      sequence: 1,
      sessionId: '20000000-0000-4000-8000-000000000001',
      workspaceId: '30000000-0000-4000-8000-000000000001',
      environmentId: '40000000-0000-4000-8000-000000000001',
      messageId: '50000000-0000-4000-8000-000000000001',
      runId: '60000000-0000-4000-8000-000000000001',
      traceId: 'a0000000-0000-4000-8000-000000000001',
      correlationId: null,
      causationId: null,
      type: 'answer_delta',
      payload: {
        delta: 'Разрешённый фрагмент',
        disclosure: {
          acceptedOutputId: '70000000-0000-4000-8000-000000000001',
          receiptId: '80000000-0000-4000-8000-000000000001',
          schemaVersion: 'qa-disclosure-receipt:v1',
          status: 'authorized',
          validationReference: '90000000-0000-4000-8000-000000000001',
        },
      },
    });
  });

  it('test_sse_batch_preserves_frame_identity_retry_and_heartbeat', () => {
    const started = wireEvent('run_started', { stage: 'accepted' });
    const heartbeat = wireEvent('heartbeat', {}, {
      event_id: '10000000-0000-4000-8000-000000000002',
      sequence: 2,
    });
    const stream = [
      'retry: 1500',
      `id: ${started.event_id}`,
      'event: run_started',
      `data: ${JSON.stringify(started)}`,
      '',
      `id: ${heartbeat.event_id}`,
      'event: heartbeat',
      `data: ${JSON.stringify(heartbeat)}`,
      '',
      '',
    ].join('\r\n');

    expect(decodeChatEventStream(stream)).toEqual({
      events: [
        expect.objectContaining({ eventId: started.event_id, type: 'run_started' }),
        expect.objectContaining({ eventId: heartbeat.event_id, type: 'heartbeat' }),
      ],
      retryMs: 1500,
    });
  });

  it('test_qa_event_v1_citation_preserves_exact_answer_and_locator_identity', () => {
    const decoded = decodeChatEvent(wireEvent('citation_added', {
      citation: {
        answer_id: '70000000-0000-4000-8000-000000000001',
        citation_id: '71000000-0000-4000-8000-000000000001',
        claim_id: '72000000-0000-4000-8000-000000000001',
        locator: {
          kind: 'character_range',
          schema_version: 'qa-event-citation-locator:v1',
          value: '12:24',
        },
        schema_version: 'qa-event-citation:v1',
        source_revision_id: '73000000-0000-4000-8000-000000000001',
      },
      disclosure: {
        accepted_output_id: '70000000-0000-4000-8000-000000000001',
        receipt_id: '74000000-0000-4000-8000-000000000001',
        schema_version: 'qa-disclosure-receipt:v1',
        status: 'authorized',
        validation_reference: '75000000-0000-4000-8000-000000000001',
      },
    }));

    expect(decoded).toMatchObject({
      type: 'citation_added',
      payload: {
        citation: {
          answerId: '70000000-0000-4000-8000-000000000001',
          citationId: '71000000-0000-4000-8000-000000000001',
          claimId: '72000000-0000-4000-8000-000000000001',
          locator: {
            kind: 'character_range',
            schemaVersion: 'qa-event-citation-locator:v1',
            value: '12:24',
          },
          schemaVersion: 'qa-event-citation:v1',
          sourceRevisionId: '73000000-0000-4000-8000-000000000001',
        },
      },
    });
  });

  it('test_qa_event_v1_rejects_citation_for_a_different_disclosed_answer', () => {
    expect(() => decodeChatEvent(wireEvent('citation_added', {
      citation: {
        answer_id: '70000000-0000-4000-8000-000000000001',
        citation_id: '71000000-0000-4000-8000-000000000001',
        claim_id: '72000000-0000-4000-8000-000000000001',
        locator: {
          kind: 'page',
          schema_version: 'qa-event-citation-locator:v1',
          value: '4',
        },
        schema_version: 'qa-event-citation:v1',
        source_revision_id: '73000000-0000-4000-8000-000000000001',
      },
      disclosure: {
        accepted_output_id: '70000000-0000-4000-8000-000000000002',
        receipt_id: '74000000-0000-4000-8000-000000000001',
        schema_version: 'qa-disclosure-receipt:v1',
        status: 'authorized',
        validation_reference: '75000000-0000-4000-8000-000000000001',
      },
    }))).toThrow(ChatProtocolError);
  });

  it.each([
    { event_id: 'not-a-uuid' },
    { correlation_id: 42 },
    { correlation_id: 'not-a-uuid' },
    { causation_id: '00000000-0000-0000-0000-000000000000' },
  ])('test_qa_event_v1_rejects_malformed_wire_identity_%j', (overrides) => {
    expect(() => decodeChatEvent(wireEvent('heartbeat', {}, overrides))).toThrow(
      ChatProtocolError,
    );
  });

  it('test_sse_batch_rejects_mismatched_frame_identity_and_wire_version', () => {
    const started = wireEvent('run_started', { stage: 'accepted' });
    expect(() => decodeChatEventStream([
      'id: different-event',
      'event: run_started',
      `data: ${JSON.stringify(started)}`,
      '',
      '',
    ].join('\n'))).toThrow(ChatProtocolError);

    expect(() => decodeChatEvent({
      ...started,
      schema_version: 'qa-event:v2',
    })).toThrow(ChatProtocolError);
  });
});

function wireEvent(
  type: ChatEvent['type'],
  payload: Record<string, unknown>,
  overrides: Record<string, unknown> = {},
) {
  return {
    schema_version: 'qa-event:v1',
    protocol_version: 1,
    event_id: '10000000-0000-4000-8000-000000000001',
    sequence: 1,
    type,
    session_id: '20000000-0000-4000-8000-000000000001',
    workspace_id: '30000000-0000-4000-8000-000000000001',
    environment_id: '40000000-0000-4000-8000-000000000001',
    message_id: '50000000-0000-4000-8000-000000000001',
    run_id: '60000000-0000-4000-8000-000000000001',
    trace_id: 'a0000000-0000-4000-8000-000000000001',
    correlation_id: null,
    causation_id: null,
    payload,
    ...overrides,
  };
}
