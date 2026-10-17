import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import { ru } from '../../i18n/resources';
import { FeedbackControls } from './FeedbackControls';

const answerId = '80000000-0000-4000-8000-000000000001';
const messageId = '50000000-0000-4000-8000-000000000001';
const runId = '70000000-0000-4000-8000-000000000001';
const feedbackId = '90000000-0000-4000-8000-000000000001';
const scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function recordedFeedback(targetId: string, targetKind: 'answer' | 'citation', outcome: 'answer.helpful' | 'answer.incorrect' | 'citation.helpful' | 'citation.incorrect') {
  return {
    schema_version: 1,
    trace_id: '60000000-0000-4000-8000-000000000001',
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    result: {
      schema_version: 1,
      status: 'recorded',
      feedback_id: feedbackId,
      feedback_version: 1,
      target_kind: targetKind,
      target_id: targetId,
      run_id: runId,
      message_id: messageId,
      citation_id: targetKind === 'citation' ? targetId : null,
      outcome,
      audit_event_id: null,
    },
  };
}

afterEach(cleanup);

describe('feedback controls', () => {
  it('test_answer_feedback_retries_same_bounded_command_without_echoing_comment', async () => {
    const requests: Array<{ key: string | null; body: unknown }> = [];
    let attempt = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init);
      requests.push({
        key: request.headers.get('Idempotency-Key'),
        body: await request.json(),
      });
      attempt += 1;
      return attempt === 1
        ? response({}, 503)
        : response(recordedFeedback(answerId, 'answer', 'answer.helpful'));
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });
    const user = userEvent.setup();
    render(
      <FeedbackControls
        client={client}
        targetKind="answer"
        targetId={answerId}
        runId={runId}
        messageId={messageId}
      />,
    );

    await user.type(screen.getByLabelText(ru.chat.feedbackCommentLabel), 'Приватный комментарий');
    await user.click(screen.getByRole('button', { name: ru.chat.feedbackHelpful }));
    expect(await screen.findByRole('alert')).toHaveTextContent(ru.chat.feedbackUnavailable);
    expect(screen.queryByRole('alert')).not.toHaveTextContent('Приватный комментарий');
    await user.click(screen.getByRole('button', { name: ru.chat.feedbackRetry }));

    expect(await screen.findByRole('status')).toHaveTextContent(ru.chat.feedbackRecorded);
    expect(requests).toHaveLength(2);
    expect(requests[0]).toEqual(requests[1]);
    expect(requests[0]?.body).toEqual({
      target_kind: 'answer',
      target_id: answerId,
      outcome: 'answer.helpful',
      run_id: runId,
      message_id: messageId,
      comment: 'Приватный комментарий',
    });
  });

  it('test_citation_controls_send_typed_incorrect_outcome_and_exact_id', async () => {
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
      return response(recordedFeedback(citationId, 'citation', 'citation.incorrect'));
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });
    const user = userEvent.setup();
    render(
      <FeedbackControls
        client={client}
        targetKind="citation"
        targetId={citationId}
        runId={runId}
        messageId={messageId}
      />,
    );

    await user.click(screen.getByRole('button', { name: ru.chat.feedbackIncorrect }));

    expect(await screen.findByRole('status')).toHaveTextContent(ru.chat.feedbackRecorded);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('test_feedback_conflict_is_localized_and_exposes_no_server_body', async () => {
    const fetcher = vi.fn<typeof fetch>(async () => response({
      detail: 'raw provider detail and private output',
    }, 409));
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });
    const user = userEvent.setup();
    render(
      <FeedbackControls
        client={client}
        targetKind="answer"
        targetId={answerId}
        runId={runId}
        messageId={messageId}
      />,
    );

    await user.click(screen.getByRole('button', { name: ru.chat.feedbackIncorrect }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(ru.chat.feedbackConflict);
    expect(alert).not.toHaveTextContent('raw provider detail');
    expect(alert).not.toHaveTextContent('private output');
  });
});
