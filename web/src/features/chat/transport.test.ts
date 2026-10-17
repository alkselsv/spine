import { describe, expect, it, vi } from 'vitest';

import {
  createControlledChatApi,
  type ControlledChatApiOptions,
  type ControlledStreamPlan,
} from './fixtures';
import {
  CHAT_PROTOCOL_VERSION,
  type ChatEvent,
  type Message,
} from './protocol';
import { SpineChatTransport } from './transport';

const scope = { workspaceId: 'workspace-1', environmentId: 'environment-1' };
const session = {
  id: 'session-1',
  ...scope,
  createdAt: '2026-10-13T10:00:00.000Z',
};
const canonicalMessage: Message = {
  id: 'message-1',
  sessionId: session.id,
  workspaceId: scope.workspaceId,
  environmentId: scope.environmentId,
  runId: 'run-1',
  role: 'assistant',
  status: 'completed',
  text: 'Канонический ответ.',
  citations: [],
  createdAt: '2026-10-13T10:00:02.000Z',
};

function chatEvent<T extends ChatEvent['type']>(
  type: T,
  payload: ChatEvent<T>['payload'],
  sequence: number,
  overrides: Partial<ChatEvent> = {},
): ChatEvent<T> {
  return {
    protocolVersion: CHAT_PROTOCOL_VERSION,
    eventId: `event-${sequence}`,
    sequence,
    sessionId: session.id,
    messageId: 'message-1',
    runId: 'run-1',
    traceId: 'trace-1',
    workspaceId: scope.workspaceId,
    environmentId: scope.environmentId,
    type,
    payload,
    ...overrides,
  } as ChatEvent<T>;
}

const runStarted = chatEvent('run_started', { stage: 'accepted' }, 1);
const completed = chatEvent('completed', { outcome: 'answered' }, 2);

function buildTransport(
  plan: ControlledStreamPlan,
  options: ConstructorParameters<typeof SpineChatTransport>[1] = {},
  fixtureOptions: Partial<Pick<ControlledChatApiOptions, 'disclosureAuthorized' | 'sessions'>> = {},
) {
  const api = createControlledChatApi({
    scope,
    sessions: fixtureOptions.sessions ?? [session],
    messages: [canonicalMessage],
    streamPlan: plan,
    ...(fixtureOptions.disclosureAuthorized === undefined
      ? {}
      : { disclosureAuthorized: fixtureOptions.disclosureAuthorized }),
  });
  return {
    api,
    transport: new SpineChatTransport(api, options),
  };
}

async function collect(events: AsyncIterable<ChatEvent>): Promise<ChatEvent[]> {
  const result: ChatEvent[] = [];
  for await (const next of events) result.push(next);
  return result;
}

describe('SpineChatTransport', () => {
  it('test_transport_supports_session_operations_and_feedback', async () => {
    const { api, transport } = buildTransport(() => [runStarted, completed]);

    await expect(transport.createSession()).resolves.toEqual(session);
    await expect(transport.getSession(session.id)).resolves.toEqual(session);
    await expect(transport.listMessages(session.id)).resolves.toEqual([canonicalMessage]);
    await expect(
      transport.submitFeedback({
        sessionId: session.id,
        messageId: canonicalMessage.id,
        workspaceId: scope.workspaceId,
        environmentId: scope.environmentId,
        rating: 'helpful',
        idempotencyKey: 'feedback-1',
      }),
    ).resolves.toBeUndefined();
    expect(api.feedbackInputs).toHaveLength(1);
  });

  it('test_transport_orders_and_deduplicates_events_before_terminal_rehydration', async () => {
    const { api, transport } = buildTransport(() => [
      completed,
      runStarted,
      runStarted,
      completed,
    ]);

    const events = await collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-1' }, new AbortController().signal),
    );

    expect(events.map(({ type }) => type)).toEqual(['run_started', 'completed']);
    expect(api.getMessageCalls).toEqual([`${session.id}:${canonicalMessage.id}`]);
    expect(transport.getCanonicalMessage(canonicalMessage.id)).toEqual(canonicalMessage);
  });

  it('test_transport_delivers_heartbeat_as_a_typed_progress_event', async () => {
    const heartbeat = chatEvent('heartbeat', {}, 2);
    const terminal = chatEvent('completed', { outcome: 'answered' }, 3);
    const { transport } = buildTransport(() => [runStarted, heartbeat, terminal]);

    const events = await collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-heartbeat' }, new AbortController().signal),
    );

    expect(events.map(({ type }) => type)).toEqual(['run_started', 'heartbeat', 'completed']);
  });

  it('test_transport_rechecks_disclosure_and_denies_before_protected_event', async () => {
    const answer = chatEvent(
      'answer_delta',
      {
        delta: 'Не должен быть показан',
        disclosure: { receiptId: 'disclosure-1', status: 'authorized' },
      },
      2,
    );
    const citation = chatEvent(
      'citation_added',
      {
        citation: {
          citationId: 'citation-1',
          claimId: 'claim-1',
          sourceRevisionId: 'revision-1',
          locator: { kind: 'page', value: '4' },
        },
        disclosure: { receiptId: 'disclosure-1', status: 'authorized' },
      },
      3,
    );
    let authorizationChecks = 0;
    const { transport } = buildTransport(
      () => [runStarted, answer, citation, completed],
      {},
      { disclosureAuthorized: () => ++authorizationChecks === 1 },
    );

    const events = await collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-auth-change' }, new AbortController().signal),
    );

    expect(events.map(({ type }) => type)).toEqual(['run_started', 'answer_delta', 'denied']);
    expect(JSON.stringify(events)).not.toContain('citation-1');
  });

  it.each([
    ['abstained', { reason: 'insufficient_evidence' }],
    ['denied', { reason: 'authorization_denied' }],
    ['failed', { reason: 'internal_failure' }],
    ['cancelled', { reason: 'request_cancelled' }],
  ] as const)('test_transport_rehydrates_message_for_%s_terminal_outcome', async (type, payload) => {
    const terminal = chatEvent(type, payload, 2);
    const { api, transport } = buildTransport(() => [runStarted, terminal]);

    await collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: `send-${type}` }, new AbortController().signal),
    );

    expect(api.getMessageCalls).toEqual([`${session.id}:${canonicalMessage.id}`]);
  });

  it('test_transport_reconnects_with_resume_cursor_after_stream_failure', async () => {
    let attempts = 0;
    const reconnectNotifications: number[] = [];
    const { api, transport } = buildTransport(() => {
      attempts += 1;
      if (attempts === 1) return failingStream([runStarted]);
      return [runStarted, completed];
    }, { maxReconnectAttempts: 2, onReconnect: (attempt) => reconnectNotifications.push(attempt) });

    const events = await collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-2' }, new AbortController().signal),
    );

    expect(events.map(({ type }) => type)).toEqual(['run_started', 'completed']);
    expect(api.streamCalls.map(({ resumeFromSequence }) => resumeFromSequence)).toEqual([0, 1]);
    expect(api.streamCalls[1]?.resumeFromEventId).toBe(runStarted.eventId);
    expect(reconnectNotifications).toEqual([1]);
  });

  it.each([
    ['workspaceId', 'workspace-other'],
    ['environmentId', 'environment-other'],
    ['id', 'message-other'],
    ['runId', 'run-other'],
  ] as const)(
    'test_transport_rejects_canonical_message_with_mismatched_%s',
    async (field, value) => {
      const { api, transport } = buildTransport(() => [runStarted, completed]);
      await collect(
        transport.sendMessage(session.id, { text: 'Первый вопрос', idempotencyKey: `send-valid-${field}` }, new AbortController().signal),
      );
      expect(transport.getCanonicalMessage(canonicalMessage.id)).toEqual(canonicalMessage);
      vi.spyOn(api, 'getMessage').mockResolvedValue({ ...canonicalMessage, [field]: value });

      const events = await collect(
        transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: `send-mismatched-${field}` }, new AbortController().signal),
      );

      expect(events.at(-1)).toMatchObject({ type: 'completed' });
      expect(transport.getCanonicalMessage(canonicalMessage.id)).toBeUndefined();
    },
  );

  it('test_transport_delivers_cancellation_while_stream_open_is_pending', async () => {
    const { api, transport } = buildTransport(() => []);
    const openMessageStream = vi.spyOn(api, 'openMessageStream').mockImplementation(
      () => new Promise(() => undefined),
    );
    const controller = new AbortController();
    const pending = collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-abort-opening' }, controller.signal),
    );
    await vi.waitFor(() => expect(openMessageStream).toHaveBeenCalledOnce());
    controller.abort();

    const events = await pending;
    expect(events.at(-1)).toMatchObject({ type: 'cancelled' });
    expect(api.cancelledRuns).toEqual([]);
  });

  it('test_transport_rejects_event_that_changes_the_identified_run', async () => {
    const conflictingTerminal = chatEvent(
      'completed',
      { outcome: 'answered' },
      2,
      { runId: 'run-other' },
    );
    const { transport } = buildTransport(() => [runStarted, conflictingTerminal]);

    const events = await collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-run-mismatch' }, new AbortController().signal),
    );

    expect(events.at(-1)).toMatchObject({ type: 'failed', payload: { reason: 'protocol_error' } });
  });

  it('test_transport_times_out_heartbeats_and_fails_closed_after_retries', async () => {
    vi.useFakeTimers();
    const { api, transport } = buildTransport(
      () => hangingStream(),
      { heartbeatTimeoutMs: 5, maxReconnectAttempts: 1 },
    );

    try {
      const pending = collect(
        transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-3' }, new AbortController().signal),
      );
      await vi.runAllTimersAsync();
      const events = await pending;

      expect(events.at(-1)).toMatchObject({ type: 'failed', payload: { reason: 'stream_timeout' } });
      expect(api.streamCalls).toHaveLength(2);
      expect(api.rehydrateCalls).toEqual([`${session.id}:command`]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('test_transport_delivers_cancellation_before_waiting_for_cancel_and_hydration', async () => {
    const { api, transport } = buildTransport(async function* () {
      yield runStarted;
      await new Promise<void>(() => undefined);
    });
    let finishCancellation: (() => void) | undefined;
    const cancelRun = vi.spyOn(api, 'cancelRun').mockImplementation(
      () => new Promise((resolve) => {
        finishCancellation = resolve;
      }),
    );
    const controller = new AbortController();
    const events = transport.sendMessage(
      session.id,
      { text: 'Вопрос', idempotencyKey: 'send-4' },
      controller.signal,
    )[Symbol.asyncIterator]();
    await expect(events.next()).resolves.toMatchObject({ done: false, value: { type: 'run_started' } });
    await vi.waitFor(() => expect(api.streamCalls).toHaveLength(1));
    controller.abort();

    await expect(events.next()).resolves.toMatchObject({ done: false, value: { type: 'cancelled' } });
    expect(cancelRun).toHaveBeenCalledWith('run-1', api.requestContext);
    finishCancellation?.();
    await expect(events.next()).resolves.toMatchObject({ done: true });
    expect(api.getMessageCalls).toEqual([`${session.id}:${canonicalMessage.id}`]);
  });

  it('test_transport_rejects_cross_scope_or_malformed_stream_without_protected_content', async () => {
    const foreignEvent = chatEvent(
      'answer_delta',
      {
        delta: 'secret answer',
        disclosure: { receiptId: 'disclosure-1', status: 'authorized' },
      },
      1,
      { sessionId: 'foreign-session' },
    );
    const { transport } = buildTransport(() => [foreignEvent]);

    const events = await collect(
      transport.sendMessage(session.id, { text: 'Вопрос', idempotencyKey: 'send-5' }, new AbortController().signal),
    );

    expect(events.at(-1)).toMatchObject({ type: 'failed', payload: { reason: 'protocol_error' } });
    expect(JSON.stringify(events)).not.toContain('secret answer');

    const foreignSession = { ...session, id: 'foreign-session', workspaceId: 'workspace-2' };
    const foreignMessage = {
      ...canonicalMessage,
      id: 'foreign-message',
      sessionId: foreignSession.id,
      workspaceId: foreignSession.workspaceId,
      environmentId: 'environment-2',
      runId: 'foreign-run',
    };
    const scopedFixture = createControlledChatApi({
      scope,
      sessions: [session, foreignSession],
      messages: [canonicalMessage, foreignMessage],
      streamPlan: () => [],
    });
    await expect(scopedFixture.getSession(session.id)).resolves.toEqual(session);
    await expect(scopedFixture.getSession(foreignSession.id)).rejects.toThrow();
    await expect(scopedFixture.getMessage(foreignSession.id, foreignMessage.id)).rejects.toThrow();
    await expect(scopedFixture.cancelRun(foreignMessage.runId, scopedFixture.requestContext)).rejects.toThrow();
  });

  it('test_transport_replays_same_idempotent_send_without_duplicate_stream_effect', async () => {
    const { api, transport } = buildTransport(() => [runStarted, completed]);
    const input = { text: 'Повторный вопрос', idempotencyKey: 'same-send' };

    const first = await collect(transport.sendMessage(session.id, input, new AbortController().signal));
    const second = await collect(transport.sendMessage(session.id, input, new AbortController().signal));

    expect(second).toEqual(first);
    expect(api.streamCalls).toHaveLength(1);
  });

  it('test_transport_replays_partial_failure_without_reissuing_idempotent_command', async () => {
    const { api, transport } = buildTransport(
      () => failingStream([runStarted]),
      { maxReconnectAttempts: 0 },
    );
    const input = { text: 'Повторный вопрос', idempotencyKey: 'partial-send' };

    const first = await collect(transport.sendMessage(session.id, input, new AbortController().signal));
    const second = await collect(transport.sendMessage(session.id, input, new AbortController().signal));

    expect(first).toEqual(second);
    expect(first.at(-1)).toMatchObject({ type: 'failed' });
    expect(api.streamCalls).toHaveLength(1);
  });

  it('test_transport_deduplicates_feedback_retry_by_idempotency_key', async () => {
    const { api, transport } = buildTransport(() => [runStarted, completed]);
    const input = {
      sessionId: session.id,
      messageId: canonicalMessage.id,
      workspaceId: scope.workspaceId,
      environmentId: scope.environmentId,
      rating: 'helpful' as const,
      idempotencyKey: 'same-feedback',
    };

    await transport.submitFeedback(input);
    await transport.submitFeedback(input);

    expect(api.feedbackInputs).toHaveLength(1);
  });

  it('test_transport_retries_feedback_after_partial_failure', async () => {
    const { api, transport } = buildTransport(() => [runStarted, completed]);
    const input = {
      sessionId: session.id,
      messageId: canonicalMessage.id,
      workspaceId: scope.workspaceId,
      environmentId: scope.environmentId,
      rating: 'helpful' as const,
      idempotencyKey: 'retry-feedback',
    };
    api.failNextFeedback = true;

    await expect(transport.submitFeedback(input)).rejects.toThrow();
    await expect(transport.submitFeedback(input)).resolves.toBeUndefined();

    expect(api.feedbackInputs).toHaveLength(1);
  });
});

async function* failingStream(events: readonly ChatEvent[]) {
  yield* events;
  throw new Error('simulated disconnect');
}

async function* hangingStream() {
  await new Promise<void>(() => undefined);
}
