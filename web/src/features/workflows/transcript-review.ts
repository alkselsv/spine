import type { components } from '../../../api/generated/schema';
import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';

export type TranscriptReviewDecision = components['schemas']['TranscriptReviewDecision'];
export type TranscriptReviewResult = components['schemas']['TranscriptReviewResponse'];
export type TranscriptReviewInspection = components['schemas']['TranscriptReviewInspection'];

export async function inspectTranscriptForReview(
  client: Pick<ScopedApiClient, 'POST'>,
  scope: Scope,
  input: { artifactId: string; artifactDigest: string },
  signal?: AbortSignal,
): Promise<TranscriptReviewInspection> {
  const { data, error } = await client.POST('/api/v1/transcript-reviews/inspect', {
    body: {
      schema_version: 1,
      transcript_artifact_id: input.artifactId,
      transcript_artifact_digest: input.artifactDigest,
    },
    signal,
  });
  if (error || !data || data.workspace_id !== scope.workspaceId || data.environment_id !== scope.environmentId) {
    throw new Error('Не удалось открыть расшифровку для проверки.');
  }
  return data.data;
}

export async function reviewTranscript(
  client: Pick<ScopedApiClient, 'POST'>,
  scope: Scope,
  input: {
    artifactId: string;
    artifactDigest: string;
    decision: TranscriptReviewDecision;
    rationale: string;
    idempotencyKey: string;
    correctedSegments?: { startMs: number; endMs: number; text: string; signals?: [] }[];
  },
): Promise<TranscriptReviewResult> {
  const { data, error } = await client.POST('/api/v1/transcript-reviews/decide', {
    body: {
      schema_version: 1,
      transcript_artifact_id: input.artifactId,
      transcript_artifact_digest: input.artifactDigest,
      decision: input.decision,
      rationale: input.rationale,
      idempotency_key: input.idempotencyKey,
      corrected_segments: input.correctedSegments?.map((segment) => ({
        start_ms: segment.startMs,
        end_ms: segment.endMs,
        text: segment.text,
        signals: segment.signals ?? [],
      })) ?? [],
    },
  });
  if (
    error || !data || data.workspace_id !== scope.workspaceId ||
    data.environment_id !== scope.environmentId
  ) {
    throw new Error('Не удалось сохранить решение проверки расшифровки.');
  }
  const result = data.data;
  if (
    result.review.input_artifact_id !== input.artifactId ||
    result.review.input_artifact_digest !== input.artifactDigest ||
    result.review.decision !== input.decision ||
    (input.decision === 'correct' && !result.corrected_artifact) ||
    (result.corrected_artifact && result.corrected_artifact.original_artifact_id !== input.artifactId)
  ) {
    throw new Error('Сервер вернул решение для другой версии расшифровки.');
  }
  return result;
}
