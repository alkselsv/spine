import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../../api/client', () => ({ createScopedApiClient: () => ({ GET: get }) }));

import { RequirementsArtifactCard } from './RequirementsArtifactCard';

afterEach(() => {
  cleanup();
  get.mockReset();
});

describe('RequirementsArtifactCard', () => {
  it('restores_partial_and_unsupported_requirement_states_from_the_api', async () => {
    get.mockResolvedValue({ data: {
      artifact_id: '60000000-0000-4000-8000-000000000001', artifact_version: 2,
      schema_version: 'requirements:v1', production_identity: 'requirements-review-v2',
      production_digest: 'a'.repeat(64), workspace_id: '10000000-0000-4000-8000-000000000001',
      environment_id: '20000000-0000-4000-8000-000000000001', work_item_id: '40000000-0000-4000-8000-000000000001',
      run_id: '30000000-0000-4000-8000-000000000001', state: 'partial', upstream_artifact_ids: [],
      predecessor_version: 1, requirements: [{ requirement_id: '70000000-0000-4000-8000-000000000001',
        schema_version: 'requirement:v1', category: 'constraint', statement: 'The request does not specify a price',
        constraints: [], uncertainty: 'No price source was provided', ambiguous_markers: ['price_basis'],
        missing_markers: ['currency'], state: 'unsupported', review_state: 'needs_review',
        confidence: 0.99, source_refs: [], evidence_refs: [], evidence_status: 'unsupported',
        upstream_artifact_ids: [] }],
    } });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RequirementsArtifactCard client={{ GET: get }} queryKey={['workspace', 'run']} runId="30000000-0000-4000-8000-000000000001" />
    </QueryClientProvider>);

    expect(await screen.findByText(/версия 2/)).toBeVisible();
    expect(screen.getByText('Статус: partial')).toBeVisible();
    expect(screen.getByText(/Не указано: currency/)).toBeVisible();
    expect(screen.getByText('Не подтверждено источниками')).toBeVisible();
    expect(screen.getByText(/Подтверждения: не подтверждено/)).toBeVisible();
    expect(get).toHaveBeenCalledWith('/api/v1/runs/{run_id}/requirements', expect.any(Object));
  });
});
