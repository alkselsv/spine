import type { components } from '../../../api/generated/schema';
import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';

export type TranscriptTimeRangeLocator = components['schemas']['TimeRangeLocator'];
export type TranscriptInspection = components['schemas']['TranscriptInspectionResult'];

export async function inspectTranscriptEvidence(
  client: Pick<ScopedApiClient, 'POST'>,
  scope: Scope,
  locator: TranscriptTimeRangeLocator,
  signal: AbortSignal,
): Promise<TranscriptInspection> {
  const { data, error } = await client.POST('/api/v1/transcript-evidence/inspect', {
    body: { schema_version: 1, locator },
    signal,
  });
  if (error || !data || data.workspace_id !== scope.workspaceId || data.environment_id !== scope.environmentId) {
    throw new Error('Transcript evidence is unavailable.');
  }

  const result = data.data;
  if (result.schema_version !== 'transcript-inspection-result:v1' || !isKnownStatus(result.status)) {
    throw new Error('Transcript evidence is unavailable.');
  }
  if (result.status !== 'current') {
    if (hasProtectedMetadata(result)) throw new Error('Transcript evidence is unavailable.');
    return result;
  }
  if (
    !result.locator ||
    result.locator.source_object_id !== locator.source_object_id ||
    result.locator.source_revision_id !== locator.source_revision_id ||
    result.locator.transcript_artifact_id !== locator.transcript_artifact_id ||
    result.locator.transcript_artifact_digest !== locator.transcript_artifact_digest ||
    result.locator.start_ms !== locator.start_ms ||
    result.locator.end_ms !== locator.end_ms ||
    result.source_revision_id !== locator.source_revision_id ||
    result.transcript_artifact_id !== locator.transcript_artifact_id ||
    !Number.isSafeInteger(result.recording_duration_ms) ||
    result.locator.end_ms > (result.recording_duration_ms ?? 0) ||
    typeof result.text !== 'string' ||
    result.text.length === 0
  ) {
    throw new Error('Transcript evidence is unavailable.');
  }
  return result;
}

function isKnownStatus(value: string): value is TranscriptInspection['status'] {
  return ['current', 'stale', 'superseded', 'review_required', 'partial', 'degraded', 'permission_denied'].includes(value);
}

function hasProtectedMetadata(value: TranscriptInspection): boolean {
  return Boolean(
    value.locator || value.source_object_id || value.source_revision_id || value.transcript_artifact_id ||
    value.transcript_artifact_digest || value.transcript_artifact_schema_version || value.publication_id ||
    value.publication_state || value.review_state || value.partial !== undefined && value.partial !== null ||
    value.recording_duration_ms !== undefined && value.recording_duration_ms !== null || value.text,
  );
}
