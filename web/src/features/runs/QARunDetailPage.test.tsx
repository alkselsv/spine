import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { QARunDetailPage } from './QARunDetailPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const runId = 'a0000000-0000-4000-8000-000000000001';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Q&A run detail', () => {
  it('test_qa_run_detail_renders_authoritative_metadata_and_sorted_safe_timeline', async () => {
    vi.stubGlobal('fetch', createRunAndCorrelationFetch());

    renderDetail(`/qa/runs/${runId}`);

    expect(await screen.findByRole('heading', { name: ru.qaRuns.detail.title })).toBeVisible();
    expect(screen.getAllByText(runId)).toHaveLength(2);
    expect(screen.getByText(/gpt-safe-2026/)).toBeVisible();
    expect(screen.getByText(/b0000000-0000-4000-8000-000000000004/)).toBeVisible();
    expect(screen.getByText(ru.chat.completeness.partiality.partial)).toBeVisible();
    expect(screen.getByText(/несовместимые подтверждённые позиции/)).toBeVisible();
    expect(screen.getByText(/target-completeness-scope:v1/)).toBeVisible();
    expect(screen.getByText(/observed-coverage:v1/)).toBeVisible();
    expect(screen.getByText('12')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: ru.diagnostics.open }));
    expect(await screen.findByText(ru.diagnostics.partial)).toBeVisible();
    expect(screen.getByRole('link', { name: ru.diagnostics.documents })).toHaveAttribute(
      'href',
      `/knowledge/documents?workspace=${scope.workspaceId}&environment=${scope.environmentId}`,
    );

    const timeline = screen.getByRole('list', { name: ru.qaRuns.detail.timeline });
    const events = within(timeline).getAllByRole('listitem');
    expect(events[0]).toHaveTextContent('started');
    expect(events[1]).toHaveTextContent('completed');
    expect(document.body).not.toHaveTextContent('secret answer or provider detail');
  });

  it('test_eligible_run_evidence_links_to_the_exact_scoped_chat_citation_and_back', async () => {
    vi.stubGlobal('fetch', createRunAndCorrelationFetch());

    renderDetail(
      `/qa/runs/${runId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&return_session_id=90000000-0000-4000-8000-000000000002&return_message_id=90000000-0000-4000-8000-000000000001`,
    );

    const citationLink = await screen.findByRole('link', { name: ru.qaRuns.detail.openCitation });
    expect(citationLink).toHaveAttribute(
      'href',
      `/chat/90000000-0000-4000-8000-000000000002?workspace=${scope.workspaceId}&environment=${scope.environmentId}&message_id=90000000-0000-4000-8000-000000000001&run_id=${runId}&citation_id=d0000000-0000-4000-8000-000000000001&return_run_id=${runId}`,
    );
    expect(screen.getByRole('link', { name: ru.qaRuns.detail.backToMessage })).toHaveAttribute(
      'href',
      `/chat/90000000-0000-4000-8000-000000000002?workspace=${scope.workspaceId}&environment=${scope.environmentId}&message_id=90000000-0000-4000-8000-000000000001&run_id=${runId}&return_run_id=${runId}`,
    );
  });

  it('test_qa_run_detail_exposes_safe_denied_state_without_provider_details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof globalThis.fetch>().mockResolvedValue(
        Response.json(
          {
            code: 'spine.authorization.denied',
            retryability: 'never',
            safe_message: 'Access denied.',
            schema_version: 1,
            trace_id: '50000000-0000-4000-8000-000000000099',
            provider_detail: 'secret answer or provider detail',
          },
          { status: 403 },
        ),
      ),
    );

    renderDetail(`/qa/runs/${runId}`);

    expect(await screen.findByRole('alert')).toHaveTextContent('Доступ запрещён');
    expect(document.body).not.toHaveTextContent('secret answer or provider detail');
  });
});

function renderDetail(initialEntry: string) {
  const router = createMemoryRouter(
    [{ path: '/qa/runs/:runId', element: <QARunDetailPage /> }],
    { initialEntries: [initialEntry] },
  );
  render(
    <AuthProvider
      value={{
        status: 'authenticated',
        role: 'administrator',
        identityVersion: 'principal-a',
        getBearerCredential: () => 'test-token',
      }}
    >
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ScopeContext.Provider
          value={{ scope, queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId] }}
        >
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>,
  );
  return router;
}

function makeDetail(): components['schemas']['QARunDetailResponse'] {
  return {
    accepted_evidence: [makeEvidence()],
    attempts: [makeAttempt()],
    causation_id: '60000000-0000-4000-8000-000000000001',
    command_digest: 'sha256:command',
    completed_at: '2026-10-16T10:00:03Z',
    correlation_id: '60000000-0000-4000-8000-000000000002',
    created_at: '2026-10-16T10:00:00Z',
    diagnostic_events: [
      {
        event_id: '70000000-0000-4000-8000-000000000002',
        kind: 'completed',
        occurred_at: '2026-10-16T10:00:03Z',
        reason: 'answer_accepted',
      },
      {
        event_id: '70000000-0000-4000-8000-000000000001',
        kind: 'started',
        occurred_at: '2026-10-16T10:00:01Z',
        reason: null,
      },
    ],
    environment_id: scope.environmentId,
    evidence_eligible: true,
    final_attempt_id: 'a1000000-0000-4000-8000-000000000001',
    input_artifact_ids: ['80000000-0000-4000-8000-000000000001'],
    message_id: '90000000-0000-4000-8000-000000000001',
    outcome: 'answered',
    output_artifact_ids: ['80000000-0000-4000-8000-000000000002'],
    run_id: runId,
    schema_version: 1,
    session_id: '90000000-0000-4000-8000-000000000002',
    state: 'completed',
    subject_id: '30000000-0000-4000-8000-000000000001',
    subject_kind: 'human',
    trace_id: '50000000-0000-4000-8000-000000000002',
    warnings: ['projection_was_stale'],
    workspace_id: scope.workspaceId,
  };
}

function makeAttempt(): components['schemas']['QARunAttemptResponse'] {
  return {
    agent_result_id: 'a2000000-0000-4000-8000-000000000001',
    attempt_id: 'a1000000-0000-4000-8000-000000000001',
    attempt_number: 1,
    completed_at: '2026-10-16T10:00:03Z',
    completeness: {
      schema_version: 'answer-completeness-presentation:v1',
      target_scope_id: 'd0000000-0000-4000-8000-000000000010',
      target_scope_version: 'target-completeness-scope:v1',
      observed_coverage_id: 'd0000000-0000-4000-8000-000000000011',
      observed_coverage_version: 'observed-coverage:v1',
      coverage_status: 'insufficient',
      partiality_status: 'partial',
      conflict_status: 'presented',
      partial_fallback_authority: 'current_request',
      serving_decision: 'serve',
      validation_decision: 'grounded_conflict',
    },
    context: {
      activation_generation: 4,
      bundle_digest: 'sha256:bundle',
      bundle_id: 'b0000000-0000-4000-8000-000000000001',
      bundle_version: 2,
      canonical_as_of_generation: 7,
      canonical_as_of_time: '2026-10-16T09:59:00Z',
      canonical_boundary_digest: 'sha256:boundary',
      canonical_boundary_id: 'b0000000-0000-4000-8000-000000000002',
      evidence_count: 12,
      freshness: 'stale',
      health: 'healthy',
      outcome: 'eligible',
      projection_activation_id: 'b0000000-0000-4000-8000-000000000003',
      projection_snapshot_id: 'b0000000-0000-4000-8000-000000000004',
      reason_code: 'accepted',
      retrieval_strategy: 'hybrid',
      state: 'ready',
    },
    context_bundle_digest: 'sha256:bundle',
    context_bundle_id: 'b0000000-0000-4000-8000-000000000001',
    context_bundle_version: 2,
    execution_pin: {
      agent_version: {
        agent_id: 'c0000000-0000-4000-8000-000000000001',
        agent_version_id: 'c0000000-0000-4000-8000-000000000002',
        configuration_generation: 3,
        digest: 'sha256:agent',
        identity_id: 'c0000000-0000-4000-8000-000000000003',
        version: 'agent-2026.10',
      },
      capability_id: 'c0000000-0000-4000-8000-000000000004',
      configuration_generation: 3,
      context_profile_binding: 'qa-default',
      context_profile_digest: 'sha256:context-profile',
      context_profile_id: 'c0000000-0000-4000-8000-000000000005',
      context_profile_version: 2,
      input_schema: 'answer-question.input.v1',
      model_profile: {
        configuration_generation: 3,
        digest: 'sha256:model',
        identity_id: 'c0000000-0000-4000-8000-000000000006',
        version: 'gpt-safe-2026',
      },
      output_schema: 'answer-question.output.v1',
      pin_digest: 'sha256:pin',
      projection: {
        activation_generation: 4,
        canonical_boundary_digest: 'sha256:boundary',
        canonical_boundary_id: 'b0000000-0000-4000-8000-000000000002',
        projection_activation_id: 'b0000000-0000-4000-8000-000000000003',
        projection_config_digest: 'sha256:projection-config',
        projection_config_version_id: 'b0000000-0000-4000-8000-000000000005',
        projection_snapshot_id: 'b0000000-0000-4000-8000-000000000004',
        snapshot_manifest_digest: 'sha256:snapshot',
      },
      prompt_version: {
        configuration_generation: 3,
        digest: 'sha256:prompt',
        identity_id: 'c0000000-0000-4000-8000-000000000007',
        version: 'prompt-2026.10',
      },
      schema_version: 'execution-pin.v1',
      semantic_support_policy: {
        configuration_generation: 3,
        digest: 'sha256:policy',
        identity_id: 'c0000000-0000-4000-8000-000000000008',
        version: 'policy-2026.10',
      },
    },
    input_artifact_ids: ['80000000-0000-4000-8000-000000000001'],
    latency_ms: 2000,
    outcome: 'answered',
    output_artifact_ids: ['80000000-0000-4000-8000-000000000002'],
    reason: 'answer_accepted',
    run_id: runId,
    schema_version: 'attempt.v1',
    started_at: '2026-10-16T10:00:01Z',
    state: 'completed',
    timeline: [],
    usage: {
      cached_input_tokens: 3,
      cost: '0.12',
      currency: 'USD',
      input_tokens: 10,
      model_invocations: 1,
      output_tokens: 20,
    },
  };
}

function makeEvidence(): components['schemas']['QARunEvidenceResponse'] {
  return {
    activation_generation: 4,
    citation_id: 'd0000000-0000-4000-8000-000000000001',
    environment_id: scope.environmentId,
    evidence_digest: 'sha256:evidence',
    evidence_id: 'evidence-1',
    locator_kind: 'page',
    locator_schema: 'page.v1',
    locator_value: '4',
    projection_snapshot_id: 'b0000000-0000-4000-8000-000000000004',
    revision_number: 2,
    source_object_id: 'e0000000-0000-4000-8000-000000000001',
    source_revision_id: 'e0000000-0000-4000-8000-000000000002',
    workspace_id: scope.workspaceId,
  };
}

function createRunAndCorrelationFetch() {
  return vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.pathname === '/api/v1/diagnostics/operational-correlation') {
      return Response.json(makeCorrelationResponse());
    }
    return Response.json({
      data: makeDetail(),
      environment_id: scope.environmentId,
      schema_version: 1,
      trace_id: '50000000-0000-4000-8000-000000000099',
      workspace_id: scope.workspaceId,
    });
  });
}

function makeCorrelationResponse(): components['schemas']['OperationalCorrelationResponse'] {
  return {
    authorization_audit_event_id: '70000000-0000-4000-8000-000000000001',
    data: {
      conflict_codes: [],
      correlation_ids: [],
      coverage: 'incomplete',
      environment_id: scope.environmentId,
      lineage: [],
      missing_sources: ['delivery_metadata'],
      records: [{
        correlation_id: '60000000-0000-4000-8000-000000000002',
        deduplication_key: `run:${runId}`,
        delivery_event_id: null,
        delivery_last_error_code: null,
        delivery_status: null,
        environment_id: scope.environmentId,
        feedback_id: null,
        feedback_outcome: null,
        feedback_target_kind: null,
        message_id: '90000000-0000-4000-8000-000000000001',
        metric_coverage: null,
        metric_currency: null,
        metric_denominator: null,
        metric_measurement: null,
        metric_numerator: null,
        metric_value: null,
        observed_at: '2026-10-16T10:00:03Z',
        run_id: runId,
        run_outcome: 'failed',
        run_state: 'completed',
        schema_version: 'operational-correlation-record:v1',
        source_identity: 'canonical.answer-question-run:v1',
        source_kind: 'canonical_run',
        source_record_id: runId,
        source_schema_version: 'answer-question-run:v1',
        trace_id: '50000000-0000-4000-8000-000000000002',
        workspace_id: scope.workspaceId,
      }],
      reference_id: runId,
      reference_kind: 'run',
      schema_version: 'operational-correlation-view:v1',
      trace_ids: ['50000000-0000-4000-8000-000000000002'],
      workspace_id: scope.workspaceId,
    },
    environment_id: scope.environmentId,
    schema_version: 1,
    trace_id: '50000000-0000-4000-8000-000000000099',
    workspace_id: scope.workspaceId,
  };
}
