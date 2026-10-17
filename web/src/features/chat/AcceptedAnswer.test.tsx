import { createHash } from 'node:crypto';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { AcceptedAnswer } from './AcceptedAnswer';
import type { QAMessage } from './session-api';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const sessionId = '30000000-0000-4000-8000-000000000001';
const messageId = '50000000-0000-4000-8000-000000000001';
const runId = '70000000-0000-4000-8000-000000000001';
const answerId = '80000000-0000-4000-8000-000000000001';
const attemptId = '90000000-0000-4000-8000-000000000001';
const citationId = 'c0000000-0000-4000-8000-000000000001';
const claimId = 'a0000000-0000-4000-8000-000000000001';
const relationshipId = 'b0000000-0000-4000-8000-000000000001';
const answer = 'Факт 😀 e\u0301 и é.';

function acceptedMessage(): QAMessage {
  return {
    schema_version: 1,
    message_id: messageId,
    session_id: sessionId,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    version: 1,
    ordinal: 1,
    trace_id: '60000000-0000-4000-8000-000000000001',
    run_id: runId,
    state: 'terminal',
    terminal_outcome: 'answered',
    cancelled: false,
    disclosure_eligibility: 'eligible',
    question: 'Синтетический вопрос',
    answer,
    accepted_presentation: {
      schema_version: 'accepted-answer-presentation:v1',
      answer_id: answerId,
      answer_text_sha256: createHash('sha256').update(answer, 'utf8').digest('hex'),
      attempt_id: attemptId,
      message_id: messageId,
      run_id: runId,
      claims: [{
        schema_version: 'accepted-answer-claim-presentation:v1',
        claim_id: claimId,
        kind: 'factual',
        start_offset: 7,
        end_offset: 12,
      }],
      relationships: [{
        schema_version: 'accepted-answer-relationship-presentation:v1',
        relationship_id: relationshipId,
        claim_id: claimId,
        kind: 'independent',
        citation_ids: [citationId],
        related_claim_id: null,
      }],
      citation_ids: [citationId],
    },
    created_at: '2026-10-17T10:00:00Z',
    updated_at: '2026-10-17T10:00:00Z',
  };
}

function citationEvidence() {
  return {
    schema_version: 'citation-inspection-evidence:v1',
    answer_id: answerId,
    citation_id: citationId,
    context_bundle: {
      schema_version: 'context-bundle-lineage-ref:v1',
      attempt_id: attemptId,
      run_id: runId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      bundle: {
        bundle_id: 'd0000000-0000-4000-8000-000000000001',
        bundle_version: 1,
        bundle_digest: 'd'.repeat(64),
      },
    },
    environment_id: scope.environmentId,
    evidence_reference: {
      schema_version: 'context-evidence-reference:v2',
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      evidence_id: 'evidence-1',
      projection_snapshot_id: 'e0000000-0000-4000-8000-000000000001',
      activation_generation: 1,
      source_object_id: 'f0000000-0000-4000-8000-000000000001',
      source_revision_id: 'a0000000-0000-4000-8000-000000000002',
      original_sha256: 'a'.repeat(64),
      revision_digest: 'b'.repeat(64),
      processing_receipt_id: 'b0000000-0000-4000-8000-000000000002',
      projected_kind: 'chunk',
      locator_contract_version: 'text:v1',
      locator: { kind: 'page', value: '4' },
      locator_digest: 'c'.repeat(64),
      evidence_digest: 'c'.repeat(64),
      status: 'valid',
    },
    excerpt: 'Защищённый синтетический фрагмент.',
    historical: false,
    locator_digest: 'c'.repeat(64),
    locator_reference: 'page:4#0-6',
    locator_schema: 'pdf-page:v1',
    original_sha256: 'a'.repeat(64),
    revision_digest: 'b'.repeat(64),
    revision_number: 2,
    source_object_id: 'f0000000-0000-4000-8000-000000000001',
    source_revision_id: 'a0000000-0000-4000-8000-000000000002',
    workspace_id: scope.workspaceId,
  };
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

afterEach(cleanup);

describe('accepted citation markers and inspection', () => {
  it('test_markdown_citation_marker_keeps_the_accepted_source_offset', async () => {
    const messageResource = acceptedMessage();
    const formattedAnswer = `**${answer}**`;
    messageResource.answer = formattedAnswer;
    messageResource.accepted_presentation!.answer_text_sha256 = createHash('sha256')
      .update(formattedAnswer, 'utf8')
      .digest('hex');
    messageResource.accepted_presentation!.claims[0]!.start_offset += 2;
    messageResource.accepted_presentation!.claims[0]!.end_offset += 2;
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: vi.fn<typeof fetch>(),
      getBearerCredential: () => 'test-token',
      scope,
    });
    const { container } = render(<AcceptedAnswer client={client} message={messageResource} scope={scope} />);

    const marker = await screen.findByRole('button', {
      name: ru.chat.citationMarkerLabel(1, ru.chat.relationshipKinds.independent),
    });
    const placement = container.querySelector('[data-claim-id]');
    expect(placement?.closest('strong')).not.toBeNull();
    expect(placement?.getAttribute('data-start-utf16')).toBe('10');
    expect(placement?.getAttribute('data-end-utf16')).toBe('15');
    expect(placement?.previousSibling?.textContent).toBe(answer.slice(0, 13));
    expect(marker).toBeVisible();
  });

  it('test_deep_linked_citation_revalidates_and_opens_current_authorized_evidence', async () => {
    const messageResource = acceptedMessage();
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${messageId}`) {
        return response({
          schema_version: 1,
          trace_id: messageResource.trace_id,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: messageResource,
        });
      }
      if (url.pathname === `/api/v1/qa/citations/${citationId}`) {
        return response({
          schema_version: 1,
          trace_id: messageResource.trace_id,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: {
            schema_version: 'citation-inspection-result:v1',
            status: 'available',
            reason: 'available',
            evidence: citationEvidence(),
          },
        });
      }
      return response({ error: 'unexpected request' }, 404);
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });

    const { container } = render(
      <AcceptedAnswer
        client={client}
        message={messageResource}
        targetCitationId={citationId}
        scope={scope}
      />,
    );

    expect(await screen.findByText('Защищённый синтетический фрагмент.')).toBeVisible();
    expect(screen.getByRole('group', { name: ru.chat.feedbackCitationLabel })).toBeVisible();
    const claimMarker = container.querySelector('[data-claim-id]');
    expect(claimMarker?.getAttribute('data-claim-id')).toBe(claimId);
    expect(claimMarker?.getAttribute('data-start-utf16')).toBe('8');
    expect(claimMarker?.getAttribute('data-end-utf16')).toBe('13');
    expect(claimMarker?.previousSibling?.textContent).toBe(answer.slice(0, 13));
    expect(screen.getByRole('button', { name: ru.chat.citationMarkerLabel(1, ru.chat.relationshipKinds.independent) })).toBeVisible();
    expect(fetcher).toHaveBeenCalledTimes(2);
    const citationRequest = fetcher.mock.calls.find(([input]) =>
      String(input instanceof Request ? input.url : input).includes(`/api/v1/qa/citations/${citationId}`),
    )?.[0];
    const request = new Request(citationRequest as RequestInfo);
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
  });

  it('test_revoked_citation_access_hides_the_whole_answer_and_graph', async () => {
    const messageResource = acceptedMessage();
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${messageId}`) {
        return response({
          schema_version: 1,
          trace_id: messageResource.trace_id,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: messageResource,
        });
      }
      if (url.pathname === `/api/v1/qa/citations/${citationId}`) {
        return response({ detail: 'private authorization diagnostic' }, 403);
      }
      return response({ error: 'unexpected request' }, 404);
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });
    const user = userEvent.setup();
    const { container } = render(<AcceptedAnswer client={client} message={messageResource} scope={scope} />);

    await user.click(await screen.findByRole('button', { name: ru.chat.citationMarkerLabel(1, ru.chat.relationshipKinds.independent) }));

    expect(await screen.findByText(ru.chat.citationUnavailable)).toBeVisible();
    expect(container.querySelector('[data-claim-id]')).toBeNull();
    expect(screen.queryByText(answer)).toBeNull();
    expect(screen.queryByRole('button', { name: /Открыть источник/ })).toBeNull();
    expect(document.body.textContent).not.toContain('private authorization diagnostic');
  });

  it('test_unavailable_inspector_response_hides_answer_and_ignores_any_excerpt', async () => {
    const messageResource = acceptedMessage();
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === `/api/v1/qa/sessions/${sessionId}/messages/${messageId}`) {
        return response({
          schema_version: 1,
          trace_id: messageResource.trace_id,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: messageResource,
        });
      }
      if (url.pathname === `/api/v1/qa/citations/${citationId}`) {
        return response({
          schema_version: 1,
          trace_id: messageResource.trace_id,
          workspace_id: scope.workspaceId,
          environment_id: scope.environmentId,
          data: {
            schema_version: 'citation-inspection-result:v1',
            status: 'unavailable',
            reason: 'citation_unavailable',
            evidence: citationEvidence(),
          },
        });
      }
      return response({ error: 'unexpected request' }, 404);
    });
    const client = createScopedApiClient({
      baseUrl: 'https://spine.test',
      fetch: fetcher,
      getBearerCredential: () => 'test-token',
      scope,
    });
    const user = userEvent.setup();
    const { container } = render(<AcceptedAnswer client={client} message={messageResource} scope={scope} />);

    await user.click(await screen.findByRole('button', { name: ru.chat.citationMarkerLabel(1, ru.chat.relationshipKinds.independent) }));

    expect(await screen.findByText(ru.chat.citationUnavailable)).toBeVisible();
    expect(screen.queryByText('Защищённый синтетический фрагмент.')).toBeNull();
    expect(screen.queryByText(answer)).toBeNull();
    expect(container.querySelector('[data-claim-id]')).toBeNull();
  });
});
