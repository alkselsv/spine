import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../../api/client', () => ({ createScopedApiClient: () => ({ GET: get, POST: post }) }));

import { RequirementsArtifactCard } from './RequirementsArtifactCard';

const requirementsArtifact = (requirement: Record<string, unknown>, overrides: Record<string, unknown> = {}) => ({
  artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 1,
  schema_version: 'requirements:v2', production_identity: 'requirements-extraction-v2',
  production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
  environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
  run_id: '30000000-0000-4000-8000-000000000001', state: 'candidate', upstream_artifact_ids: [],
  predecessor_version: null, clarifications: [], requirements: [requirement], ...overrides,
});

afterEach(() => {
  cleanup();
  get.mockReset();
  post.mockReset();
});

describe('RequirementsArtifactCard review controls', () => {
  it('submits_an_exact_version_edit_and_refreshes_the_conversation_card', async () => {
    const artifact = {
      artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 1,
      schema_version: 'requirements:v1', production_identity: 'requirements-review-v1',
      production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
      environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
      run_id: '30000000-0000-4000-8000-000000000001', state: 'candidate', upstream_artifact_ids: [],
      predecessor_version: null, clarifications: [], requirements: [{ requirement_id: '70000000-0000-4000-8000-000000000001',
        schema_version: 'requirement:v1', category: 'functional', statement: 'Friday', constraints: [], uncertainty: null,
        ambiguous_markers: [], missing_markers: [], state: 'candidate', review_state: 'needs_review', confidence: null,
        source_refs: [], evidence_refs: [], evidence_status: 'unverified', upstream_artifact_ids: [] }],
    } as const;
    get.mockResolvedValue({ data: artifact });
    post.mockResolvedValue({ data: { action: 'edit_requirement' } });
    const refresh = vi.fn().mockResolvedValue(undefined);

    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard
        allowedCommands={['edit_requirement', 'save_review', 'request_rework']}
        client={{ GET: get, POST: post }}
        onRefresh={refresh}
        queryKey={['workspace', 'run']}
        runId={artifact.run_id}
        runVersion={4}
      />
    </QueryClientProvider>);

    await userEvent.click(await screen.findByRole('button', { name: 'Редактировать требование' }));
    const statement = screen.getByLabelText('Формулировка требования');
    await userEvent.clear(statement);
    await userEvent.type(statement, 'Friday by noon');
    await userEvent.type(screen.getByLabelText('Причина изменения'), 'Уточнён срок');
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить изменение' }));

    expect(post).toHaveBeenCalledWith('/api/v1/runs/{run_id}/commands', expect.objectContaining({
      body: expect.objectContaining({
        command_type: 'edit_requirement', expected_run_version: 4,
        artifact_refs: [expect.objectContaining({ artifact_id: artifact.artifact_id, artifact_version: 1 })],
        payload: expect.objectContaining({
          requirement_id: artifact.requirements[0].requirement_id,
          statement: 'Friday by noon', reason: 'Уточнён срок',
        }),
      }),
    }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it('creates_an_explicit_assumption_successor_from_fresh_human_text', async () => {
    const artifact = requirementsArtifact({
      requirement_id: '70000000-0000-4000-8000-000000000001',
      schema_version: 'requirement:v2', category: 'functional', statement: 'Protected source text',
      constraints: [], uncertainty: null, ambiguous_markers: ['deadline'], missing_markers: [],
      state: 'partial', review_state: 'reviewed', confidence: null, source_refs: [],
      evidence_refs: [], evidence_status: 'unsupported', upstream_artifact_ids: [],
    });
    get.mockResolvedValue({ data: artifact });
    post.mockResolvedValue({ data: { action: 'reclassify_requirement' } });

    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard
        allowedCommands={['reclassify_requirement'] as never}
        client={{ GET: get, POST: post }} queryKey={['workspace', 'run']}
        runId={artifact.run_id} runVersion={6}
      />
    </QueryClientProvider>);

    await userEvent.click(await screen.findByRole('button', { name: 'Переклассифицировать требование' }));
    await userEvent.selectOptions(screen.getByLabelText('Основание'), 'explicit_assumption');
    await userEvent.type(screen.getByLabelText('Новая независимая формулировка'), 'Our planning assumption is Friday noon.');
    await userEvent.type(screen.getByLabelText('Обоснование'), 'Needed for delivery planning.');
    await userEvent.type(screen.getByLabelText('Последствие, если допущение неверно'), 'The plan must be revised.');
    await userEvent.type(screen.getByLabelText('Решение конфликта (если применимо)'), 'Use the explicit planning assumption.');
    await userEvent.click(screen.getByRole('checkbox', { name: /написал новую формулировку/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Создать новую версию' }));

    expect(post).toHaveBeenCalledWith('/api/v1/runs/{run_id}/commands', expect.objectContaining({
      body: expect.objectContaining({
        command_type: 'reclassify_requirement', expected_run_version: 6,
        artifact_refs: [expect.objectContaining({ artifact_version: 1 })],
        payload: expect.objectContaining({
          acceptance_basis: 'explicit_assumption',
          statement: 'Our planning assumption is Friday noon.',
          rationale: 'Needed for delivery planning.',
          independence_attestation: 'authored_from_independent_knowledge_without_protected_text',
          false_consequence: 'The plan must be revised.',
          conflict_resolution_rationale: 'Use the explicit planning assumption.',
        }),
      }),
    }));
  });

  it('offers_refresh_after_a_stale_rework_conflict_and_hides_controls_for_terminal_runs', async () => {
    const artifact = {
      artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 2,
      schema_version: 'requirements:v1', production_identity: 'requirements-review-v2',
      production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
      environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
      run_id: '30000000-0000-4000-8000-000000000001', state: 'candidate', upstream_artifact_ids: [],
      predecessor_version: 1, clarifications: [], requirements: [{ requirement_id: '70000000-0000-4000-8000-000000000001',
        schema_version: 'requirement:v1', category: 'functional', statement: 'Friday', constraints: [], uncertainty: null,
        ambiguous_markers: [], missing_markers: [], state: 'candidate', review_state: 'needs_review', confidence: null,
        source_refs: [], evidence_refs: [], evidence_status: 'unverified', upstream_artifact_ids: [] }],
    } as const;
    const refreshedArtifact = { ...artifact, artifact_version: 3, predecessor_version: 2 } as const;
    get.mockResolvedValueOnce({ data: artifact }).mockResolvedValue({ data: refreshedArtifact });
    post
      .mockResolvedValueOnce({ error: { code: 'requirements.artifact_stale' } })
      .mockResolvedValueOnce({ data: { action: 'request_rework' } });
    const refresh = vi.fn().mockResolvedValue(undefined);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={queryClient}>
      <RequirementsArtifactCard
        allowedCommands={['request_rework']}
        client={{ GET: get, POST: post }}
        onRefresh={refresh}
        queryKey={['workspace', 'run']}
        runId={artifact.run_id}
        runVersion={5}
      />
    </QueryClientProvider>);

    await userEvent.type(await screen.findByLabelText('Причина решения'), 'Нужна новая проверка');
    await userEvent.click(screen.getByRole('button', { name: 'Вернуть на доработку' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('устарела');
    const staleCommandId = post.mock.calls[0]?.[1]?.body?.command_id;
    await userEvent.click(screen.getByRole('button', { name: 'Обновить версию' }));
    expect(refresh).toHaveBeenCalledOnce();
    await screen.findByText(/версия 3/);
    view.rerender(<QueryClientProvider client={queryClient}>
      <RequirementsArtifactCard
        allowedCommands={['request_rework']}
        client={{ GET: get, POST: post }}
        onRefresh={refresh}
        queryKey={['workspace', 'run']}
        runId={artifact.run_id}
        runVersion={6}
      />
    </QueryClientProvider>);
    await userEvent.click(screen.getByRole('button', { name: 'Вернуть на доработку' }));
    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[1]?.[1]?.body).toEqual(expect.objectContaining({
      expected_run_version: 6,
      artifact_refs: [expect.objectContaining({ artifact_version: 3 })],
    }));
    expect(post.mock.calls[1]?.[1]?.body?.command_id).not.toBe(staleCommandId);

    view.unmount();
    render(<QueryClientProvider client={new QueryClient()}>
      <RequirementsArtifactCard
        allowedCommands={[]}
        client={{ GET: get, POST: post }}
        queryKey={['workspace', 'terminal-run']}
        runId={artifact.run_id}
        runVersion={6}
      />
    </QueryClientProvider>);
    expect(await screen.findByText(/Friday/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Редактировать требование' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Вернуть на доработку' })).toBeNull();
  });

  it('renders_permission_denied_for_an_unauthorized_review_command', async () => {
    const artifact = {
      artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 1,
      schema_version: 'requirements:v1', production_identity: 'requirements-review-v1',
      production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
      environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
      run_id: '30000000-0000-4000-8000-000000000001', state: 'candidate', upstream_artifact_ids: [],
      predecessor_version: null, clarifications: [], requirements: [],
    } as const;
    get.mockResolvedValue({ data: artifact });
    post.mockResolvedValue({ error: { code: 'spine.authorization.denied' } });

    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard
        allowedCommands={['save_review']}
        client={{ GET: get, POST: post }}
        queryKey={['workspace', 'run']}
        runId={artifact.run_id}
        runVersion={1}
      />
    </QueryClientProvider>);

    await userEvent.type(await screen.findByLabelText('Причина решения'), 'Проверено');
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить проверку' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Недостаточно прав');
    expect(screen.queryByText(/безопасно повторить/)).toBeNull();
  });

  it('submits_exact_acceptance_and_refreshes_after_a_conflict', async () => {
    const artifact = requirementsArtifact({
      requirement_id: '70000000-0000-4000-8000-000000000001',
      schema_version: 'requirement:v3', category: 'functional', statement: 'Friday',
      constraints: [], uncertainty: null, ambiguous_markers: [], missing_markers: [],
      state: 'human_reviewed', review_state: 'reviewed', confidence: null,
      source_refs: [], evidence_refs: [], evidence_status: 'referenced', upstream_artifact_ids: [],
      acceptance_basis: 'manager_assertion', acceptance_rationale: 'Confirmed by manager',
      false_consequence: null, attributed_by: { kind: 'human', id: '20000000-0000-4000-8000-000000000071' },
      assertion_audit_reference: '64500000-0000-4000-8000-000000000010',
      independent_human_assertion: true, conflict_resolution_rationale: null,
    }, { state: 'human_reviewed' });
    get.mockResolvedValue({ data: artifact });
    post.mockResolvedValue({ error: { code: 'workflow.run_stale' } });
    const refresh = vi.fn().mockResolvedValue(undefined);

    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard
        allowedCommands={['accept_requirements'] as never}
        client={{ GET: get, POST: post }}
        onRefresh={refresh}
        queryKey={['workspace', 'run']}
        runId={artifact.run_id}
        runVersion={7}
      />
    </QueryClientProvider>);

    expect(await screen.findByText('Digest версии:', { exact: false })).toBeVisible();
    expect(screen.getByText(artifact.production_digest, { selector: 'code' })).toBeVisible();
    expect(screen.getByText('Утверждения менеджера: 1. Явные допущения: 0.')).toBeVisible();
    const accept = screen.getByRole('button', { name: 'Принять требования' });
    expect(accept).toBeDisabled();
    await userEvent.click(screen.getByRole('checkbox', { name: /Подтверждаю эти количества/ }));
    await userEvent.click(accept);

    expect(post).toHaveBeenCalledWith('/api/v1/runs/{run_id}/commands', expect.objectContaining({
      body: expect.objectContaining({
        command_type: 'accept_requirements', expected_run_version: 7,
        artifact_refs: [expect.objectContaining({
          artifact_id: artifact.artifact_id, artifact_version: 1,
        })],
        payload: expect.objectContaining({
          artifact_digest: artifact.production_digest,
          policy_version: 'requirements-artifact-acceptance:v1',
          acknowledged_assertion_count: 1,
          acknowledged_assumption_count: 0,
        }),
      }),
    }));
    expect(await screen.findByRole('alert')).toHaveTextContent('изменились');
    await userEvent.click(screen.getByRole('button', { name: 'Обновить версию' }));
    expect(refresh).toHaveBeenCalledOnce();
  });
});

describe('RequirementsArtifactCard', () => {
  it('answers_the_persisted_clarification_on_the_same_run_and_artifact_version', async () => {
    const artifact = {
      artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 1,
      schema_version: 'requirements:v1', production_identity: 'requirements-extraction-v1',
      production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
      environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
      run_id: '30000000-0000-4000-8000-000000000001', state: 'partial', upstream_artifact_ids: [],
      predecessor_version: null, requirements: [{
        requirement_id: '70000000-0000-4000-8000-000000000001', schema_version: 'requirement:v1',
        category: 'unresolved_information', statement: 'Delivery date is missing', constraints: [], uncertainty: 'Missing date',
        ambiguous_markers: [], missing_markers: ['delivery_date'], state: 'partial', review_state: 'needs_review',
        confidence: null, source_refs: [], evidence_refs: [], evidence_status: 'unverified', upstream_artifact_ids: [],
      }],
      clarifications: [{
        schema_version: 'clarification:v1', clarification_id: '64300000-0000-4000-8000-000000000001',
        run_id: '30000000-0000-4000-8000-000000000001', requirement_id: '70000000-0000-4000-8000-000000000001',
        originating_artifact_id: '60000000-0000-4000-8000-000000000001', originating_artifact_version: 1,
        question: 'Please provide the delivery deadline.', reason: 'missing_information',
        asked_by: { kind: 'agent', id: '64300000-0000-4000-8000-000000000002' },
        asked_event_id: '64300000-0000-4000-8000-000000000003', asked_at: '2026-10-29T09:00:00Z',
        correlation_id: '64300000-0000-4000-8000-000000000004', answer: null,
        resulting_artifact_version: null, affected_validation_references: [],
      }],
    };
    get.mockResolvedValue({ data: artifact });
    post
      .mockRejectedValueOnce(new Error('response lost after command commit'))
      .mockResolvedValueOnce({ data: { run_id: artifact.run_id, run_version: 3 } });
    const onAnswered = vi.fn().mockResolvedValue(undefined);
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard
        canAnswerClarification
        client={{ GET: get, POST: post }}
        onAnswered={onAnswered}
        queryKey={['workspace', 'run']}
        runId={artifact.run_id}
        runVersion={2}
      />
    </QueryClientProvider>);

    await userEvent.type(await screen.findByLabelText('Ответ менеджера'), 'December 20.');
    await userEvent.click(screen.getByRole('button', { name: 'Ответить и продолжить' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось сохранить ответ');
    await userEvent.click(screen.getByRole('button', { name: 'Ответить и продолжить' }));

    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[1]).toEqual(post.mock.calls[0]);
    expect(post).toHaveBeenLastCalledWith(
      '/api/v1/runs/{run_id}/commands',
      expect.objectContaining({ body: expect.objectContaining({
        command_type: 'answer_clarification', expected_run_version: 2,
        payload: expect.objectContaining({
          clarification_id: '64300000-0000-4000-8000-000000000001',
          answer: 'December 20.',
        }),
        artifact_refs: [expect.objectContaining({ artifact_version: 1 })],
      }) }),
    );
    expect(onAnswered).toHaveBeenCalledOnce();
  });

  it('restores_partial_and_unsupported_requirement_states_from_the_api', async () => {
    get.mockResolvedValue({ data: {
      artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 2,
      schema_version: 'requirements:v1', production_identity: 'requirements-review-v2',
      production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
      environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
      run_id: '30000000-0000-4000-8000-000000000001', state: 'partial', upstream_artifact_ids: [],
      predecessor_version: 1, clarifications: [], requirements: [{ requirement_id: '70000000-0000-4000-8000-000000000001',
        schema_version: 'requirement:v1', category: 'constraint', statement: 'The request does not specify a price',
        constraints: [], uncertainty: 'No price source was provided', ambiguous_markers: ['price_basis'],
        missing_markers: ['currency'], state: 'unsupported', review_state: 'needs_review',
        confidence: 0.99, source_refs: [], evidence_refs: [], evidence_status: 'unsupported',
        upstream_artifact_ids: [] }],
    } });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard client={{ GET: get, POST: post }} queryKey={['workspace', 'run']} runId="30000000-0000-4000-8000-000000000001" />
    </QueryClientProvider>);

    expect(await screen.findByText(/версия 2/)).toBeVisible();
    expect(screen.getByText('Статус: partial')).toBeVisible();
    expect(screen.getByText(/Не указано: currency/)).toBeVisible();
    expect(screen.getByText('Не подтверждено источниками')).toBeVisible();
    expect(screen.getByText(/Подтверждения: не подтверждено/)).toBeVisible();
    expect(get).toHaveBeenCalledWith('/api/v1/runs/{run_id}/requirements', expect.any(Object));
  });

  it('opens_the_authorized_evidence_inspector_without_falling_back_to_cached_text', async () => {
    const artifact = {
      artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 2,
      schema_version: 'requirements:v1', production_identity: 'requirements-review-v2',
      production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
      environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
      run_id: '30000000-0000-4000-8000-000000000001', state: 'candidate', upstream_artifact_ids: [],
      predecessor_version: 1, clarifications: [], requirements: [{ requirement_id: '70000000-0000-4000-8000-000000000001',
        schema_version: 'requirement:v1', category: 'functional', statement: 'Friday',
        constraints: [], uncertainty: null, ambiguous_markers: [], missing_markers: [], state: 'candidate',
        review_state: 'needs_review', confidence: 0.9, source_refs: [{
          source_object_id: '80000000-0000-4000-8000-000000000001',
          source_revision_id: '80000000-0000-4000-8000-000000000002',
          provenance_id: '80000000-0000-4000-8000-000000000003',
        }], evidence_refs: [{
          source_ref: {
            source_object_id: '80000000-0000-4000-8000-000000000001',
            source_revision_id: '80000000-0000-4000-8000-000000000002',
            provenance_id: '80000000-0000-4000-8000-000000000003',
          },
          parsed_artifact_ref: { id: '80000000-0000-4000-8000-000000000004', schema_name: 'parsed-document', schema_version: 'parsed-document:v1' },
          segment_id: '80000000-0000-4000-8000-000000000005', segment_digest: 'b'.repeat(64),
          locator: { kind: 'physical_line', line_number: 4, start_codepoint: 0, end_codepoint: 6 },
          relationship: 'supports', validation: null,
        }], evidence_status: 'unverified', upstream_artifact_ids: [] }],
    };
    get.mockImplementation(async (path: string) => path.includes('/evidence/')
      ? { data: { data: {
        schema_version: 'requirements-evidence-inspection-response:v1', status: 'available',
        evidence: {
          schema_version: 'requirements-evidence-inspection-evidence:v1',
          source_object_id: '80000000-0000-4000-8000-000000000001',
          source_revision_id: '80000000-0000-4000-8000-000000000002',
          segment_id: '80000000-0000-4000-8000-000000000005', segment_digest: 'b'.repeat(64),
          locator: { kind: 'physical_line', line_number: 4, start_codepoint: 0, end_codepoint: 6 },
          text: 'current protected evidence', validation_reference: '80000000-0000-4000-8000-000000000006',
          authorization_decision_reference: '80000000-0000-4000-8000-000000000007',
        },
      } } }
      : { data: artifact });

    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard client={{ GET: get, POST: post }} queryKey={['workspace', 'run']} runId={artifact.run_id} />
    </QueryClientProvider>);

    expect(await screen.findByText(
      'SourceRevision: 80000000-0000-4000-8000-000000000002',
    )).toBeVisible();
    await userEvent.click(await screen.findByRole('button', { name: /evidence/ }));
    expect(await screen.findByText('current protected evidence')).toBeVisible();
    expect(get).toHaveBeenCalledWith(
      '/api/v1/artifacts/{artifact_id}/requirements/{requirement_id}/evidence/{segment_id}',
      expect.any(Object),
    );
  });

  it.each(['denied', 'stale', 'degraded'] as const)('renders_%s_evidence_without_protected_text', async (status) => {
    get.mockImplementation(async (path: string) => path.includes('/evidence/')
      ? { data: { data: { schema_version: 'requirements-evidence-inspection-response:v1', status, evidence: null } } }
      : { data: {
        artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 1,
        schema_version: 'requirements:v1', production_identity: 'requirements-review-v1', production_digest: 'a'.repeat(64),
        workspace_id: '10000000-0000-4000-8000-000000000001', environment_id: '20000000-0000-4000-8000-000000000001',
        work_item_id: '40000000-0000-4000-8000-000000000001', run_id: '30000000-0000-4000-8000-000000000001', state: 'candidate',
        upstream_artifact_ids: [], predecessor_version: null, clarifications: [], requirements: [{ requirement_id: '70000000-0000-4000-8000-000000000001',
          schema_version: 'requirement:v1', category: 'functional', statement: 'Friday', constraints: [], uncertainty: null,
          ambiguous_markers: [], missing_markers: [], state: 'candidate', review_state: 'needs_review', confidence: null,
          source_refs: [{ source_object_id: '80000000-0000-0000-0000-000000000001', source_revision_id: '80000000-0000-0000-0000-000000000002', provenance_id: '80000000-0000-0000-0000-000000000003' }],
          evidence_refs: [{ source_ref: { source_object_id: '80000000-0000-0000-0000-000000000001', source_revision_id: '80000000-0000-0000-0000-000000000002', provenance_id: '80000000-0000-0000-0000-000000000003' }, parsed_artifact_ref: { id: '80000000-0000-0000-0000-000000000004', schema_name: 'parsed-document', schema_version: 'parsed-document:v1' }, segment_id: '80000000-0000-0000-0000-000000000005', segment_digest: 'b'.repeat(64), locator: { kind: 'physical_line', line_number: 1, start_codepoint: 0, end_codepoint: 6 }, relationship: 'supports', validation: null }], evidence_status: 'unverified', upstream_artifact_ids: [] }],
      } });

    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard client={{ GET: get, POST: post }} queryKey={['workspace', 'run']} runId="30000000-0000-4000-8000-000000000001" />
    </QueryClientProvider>);

    await userEvent.click(await screen.findByRole('button', { name: /evidence/ }));
    expect(await screen.findByText(new RegExp(status))).toBeVisible();
    expect(screen.queryByText('protected text')).toBeNull();
  });

  it('opens_exact_transcript_time_evidence_from_the_existing_run_card', async () => {
    const locator = {
      schema_version: 'time-range-locator:v1',
      source_object_id: '80000000-0000-4000-8000-000000000001',
      source_revision_id: '80000000-0000-4000-8000-000000000002',
      source_revision_digest: 'a'.repeat(64),
      transcript_artifact_id: '80000000-0000-4000-8000-000000000004',
      transcript_artifact_digest: 'b'.repeat(64),
      transcript_artifact_schema_version: 'transcription-artifact:v1',
      projection_version: 'transcript-projection:v1',
      start_ms: 1_250,
      end_ms: 2_750,
      segment_ids: ['80000000-0000-4000-8000-000000000005'],
    } as const;
    const artifact = requirementsArtifact({ requirement_id: '70000000-0000-4000-8000-000000000001',
        schema_version: 'requirement:v2', category: 'schedule', statement: 'Launch by 20 December', constraints: [], uncertainty: null,
        ambiguous_markers: [], missing_markers: [], state: 'candidate', review_state: 'needs_review', confidence: null,
        source_refs: [{ source_object_id: locator.source_object_id, source_revision_id: locator.source_revision_id,
          provenance_id: '80000000-0000-4000-8000-000000000003' }],
        evidence_refs: [{ source_ref: { source_object_id: locator.source_object_id, source_revision_id: locator.source_revision_id,
          provenance_id: '80000000-0000-4000-8000-000000000003' }, parsed_artifact_ref: null,
          transcript_locator: locator, segment_id: locator.segment_ids[0], segment_digest: 'c'.repeat(64),
          locator_contract_version: 'time-range-locator:v1', locator: null, relationship: 'supports', validation: null }],
        evidence_status: 'referenced', upstream_artifact_ids: [] });
    get.mockResolvedValue({ data: artifact });
    post.mockResolvedValue({ data: {
      workspace_id: artifact.workspace_id, environment_id: artifact.environment_id,
      data: { schema_version: 'transcript-inspection-result:v1', status: 'current', locator,
        source_object_id: locator.source_object_id, source_revision_id: locator.source_revision_id,
        transcript_artifact_id: locator.transcript_artifact_id,
        transcript_artifact_digest: locator.transcript_artifact_digest,
        transcript_artifact_schema_version: locator.transcript_artifact_schema_version,
        publication_id: '90000000-0000-4000-8000-000000000001', publication_state: 'active',
        review_state: 'approved', partial: false, recording_duration_ms: 10_000,
        text: 'Launch by 20 December' },
    } });

    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard client={{ GET: get, POST: post }} queryKey={['workspace', 'run']}
        runId={artifact.run_id} scope={{ workspaceId: artifact.workspace_id, environmentId: artifact.environment_id }} />
    </QueryClientProvider>);

    await userEvent.click(await screen.findByRole('button', { name: 'Открыть фрагмент расшифровки' }));
    expect(await screen.findByText('Launch by 20 December')).toBeVisible();
    expect(screen.getByText('00:01.250–00:02.750')).toBeVisible();
    expect(post).toHaveBeenCalledWith('/api/v1/transcript-evidence/inspect', expect.objectContaining({
      body: { schema_version: 1, locator },
    }));
  });
});
