import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import userEvent from '@testing-library/user-event';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Scope } from '../../app/scope';
import type { components } from '../../../api/generated/schema';
import { TranscriptEvidenceInspector } from './TranscriptEvidenceInspector';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const locator: components['schemas']['TimeRangeLocator'] = {
  schema_version: 'time-range-locator:v1',
  source_object_id: '30000000-0000-4000-8000-000000000001',
  source_revision_id: '40000000-0000-4000-8000-000000000001',
  source_revision_digest: 'a'.repeat(64),
  transcript_artifact_id: '50000000-0000-4000-8000-000000000001',
  transcript_artifact_digest: 'b'.repeat(64),
  transcript_artifact_schema_version: 'transcription-artifact:v1',
  projection_version: 'transcript-projection:v1',
  start_ms: 1_000,
  end_ms: 2_500,
  segment_ids: ['60000000-0000-4000-8000-000000000001'],
} as const;

afterEach(cleanup);

describe('TranscriptEvidenceInspector', () => {
  it('shows_exact_time_revision_version_publication_and_review_state', async () => {
    const post = vi.fn().mockResolvedValue({ data: {
      schema_version: 1,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      trace_id: '80000000-0000-4000-8000-000000000001',
      data: {
        schema_version: 'transcript-inspection-result:v1', status: 'current', locator,
        source_object_id: locator.source_object_id,
        source_revision_id: locator.source_revision_id,
        transcript_artifact_id: locator.transcript_artifact_id,
        transcript_artifact_digest: locator.transcript_artifact_digest,
        transcript_artifact_schema_version: locator.transcript_artifact_schema_version,
        publication_id: '70000000-0000-4000-8000-000000000001', publication_state: 'active',
        review_state: 'approved', partial: false, recording_duration_ms: 10_000,
        text: 'Сегмент расшифровки.',
      },
    } });
    const navigate = vi.fn();
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TranscriptEvidenceInspector
        client={{ POST: post } as never}
        locator={locator}
        onNavigate={navigate}
        scope={scope}
      />
    </QueryClientProvider>);

    await userEvent.click(screen.getByRole('button', { name: 'Открыть фрагмент расшифровки' }));

    expect(await screen.findByText('Сегмент расшифровки.')).toBeVisible();
    expect(screen.getByText('00:01.000–00:02.500')).toBeVisible();
    expect(screen.getByText(locator.source_revision_id)).toBeVisible();
    expect(screen.getByText(/Одобрена/)).toBeVisible();
    expect(post).toHaveBeenCalledWith('/api/v1/transcript-evidence/inspect', expect.objectContaining({
      body: { schema_version: 1, locator },
    }));

    await userEvent.click(screen.getByRole('button', { name: 'Перейти к фрагменту записи' }));
    expect(navigate).toHaveBeenCalledWith(1_000, 2_500);
  });

  it('renders_revocation_and_partial_states_without_cached_transcript_text', async () => {
    const post = vi.fn().mockResolvedValue({ data: {
      schema_version: 1,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      trace_id: '80000000-0000-4000-8000-000000000001',
      data: { schema_version: 'transcript-inspection-result:v1', status: 'permission_denied' },
    } });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TranscriptEvidenceInspector client={{ POST: post } as never} locator={locator} scope={scope} />
    </QueryClientProvider>);

    await userEvent.click(screen.getByRole('button', { name: 'Открыть фрагмент расшифровки' }));
    expect(await screen.findByText('Доступ к записи отозван или запрещён.')).toBeVisible();
    expect(screen.queryByText('Сегмент расшифровки.')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Перейти к фрагменту записи' })).toBeNull();
  });

  it('hides_cached_transcript_text_when_access_is_revoked_on_recheck', async () => {
    const post = vi.fn()
      .mockResolvedValueOnce({ data: {
        schema_version: 1,
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        trace_id: '80000000-0000-4000-8000-000000000001',
        data: {
          schema_version: 'transcript-inspection-result:v1', status: 'current', locator,
          source_object_id: locator.source_object_id,
          source_revision_id: locator.source_revision_id,
          transcript_artifact_id: locator.transcript_artifact_id,
          transcript_artifact_digest: locator.transcript_artifact_digest,
          transcript_artifact_schema_version: locator.transcript_artifact_schema_version,
          publication_id: '70000000-0000-4000-8000-000000000001', publication_state: 'active',
          review_state: 'approved', partial: false, recording_duration_ms: 10_000,
          text: 'Сегмент расшифровки.',
        },
      } })
      .mockRejectedValueOnce(new Error('permission revoked'));
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TranscriptEvidenceInspector client={{ POST: post } as never} locator={locator} scope={scope} />
    </QueryClientProvider>);

    await userEvent.click(screen.getByRole('button', { name: 'Открыть фрагмент расшифровки' }));
    expect(await screen.findByText('Сегмент расшифровки.')).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: 'Проверить доступ снова' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось проверить доступ');
    expect(screen.queryByText('Сегмент расшифровки.')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Перейти к фрагменту записи' })).toBeNull();
  });

  it('submits_an_immutable_correction_for_the_exact_asr_artifact', async () => {
    const post = vi.fn()
      .mockResolvedValueOnce({ data: {
        schema_version: 1, workspace_id: scope.workspaceId, environment_id: scope.environmentId,
        trace_id: '80000000-0000-4000-8000-000000000001',
        data: {
          schema_version: 'transcript-inspection-result:v1', status: 'current', locator,
          source_object_id: locator.source_object_id,
          source_revision_id: locator.source_revision_id,
          transcript_artifact_id: locator.transcript_artifact_id,
          transcript_artifact_digest: locator.transcript_artifact_digest,
          transcript_artifact_schema_version: locator.transcript_artifact_schema_version,
          publication_id: '70000000-0000-4000-8000-000000000001', publication_state: 'active',
          review_state: 'not_required', partial: false, recording_duration_ms: 10_000,
          text: 'Сегмент расшифровки.',
        },
      } })
      .mockResolvedValueOnce({ data: {
        schema_version: 1, workspace_id: scope.workspaceId, environment_id: scope.environmentId,
        trace_id: '80000000-0000-4000-8000-000000000001',
        data: { review: {
          decision: 'correct', input_artifact_id: locator.transcript_artifact_id,
          input_artifact_digest: locator.transcript_artifact_digest, publication_eligible: true,
        }, corrected_artifact: { original_artifact_id: locator.transcript_artifact_id } },
      } });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TranscriptEvidenceInspector client={{ POST: post } as never} locator={locator} scope={scope} />
    </QueryClientProvider>);

    await userEvent.click(screen.getByRole('button', { name: 'Открыть фрагмент расшифровки' }));
    await screen.findByRole('status');
    await userEvent.clear(screen.getByLabelText('Исправление фрагмента'));
    await userEvent.type(screen.getByLabelText('Исправление фрагмента'), 'Исправленная фраза');
    await userEvent.click(screen.getByRole('button', { name: 'Исправить и сохранить' }));

    expect(await screen.findByText('Создана исправленная версия расшифровки.')).toBeVisible();
    expect(post).toHaveBeenNthCalledWith(2, '/api/v1/transcript-reviews/decide', expect.objectContaining({
      body: expect.objectContaining({
        transcript_artifact_id: locator.transcript_artifact_id,
        transcript_artifact_digest: locator.transcript_artifact_digest,
        decision: 'correct',
        corrected_segments: [{ start_ms: locator.start_ms, end_ms: locator.end_ms, text: 'Исправленная фраза', signals: [] }],
      }),
    }));
  });
});
