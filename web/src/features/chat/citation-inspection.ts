import type { components } from '../../../api/generated/schema';
import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';

type CitationInspectionEvidence = components['schemas']['CitationInspectionEvidence'];

export type CitationInspection =
  | { status: 'available'; evidence: CitationInspectionEvidence }
  | { status: 'unavailable' };

export interface AcceptedAnswerLineage {
  answerId: string;
  attemptId: string;
  runId: string;
}

export async function inspectAcceptedCitation(
  client: ScopedApiClient,
  scope: Scope,
  lineage: AcceptedAnswerLineage,
  citationId: string,
  signal: AbortSignal,
): Promise<CitationInspection> {
  const response = await client.GET('/api/v1/qa/citations/{citation_id}', {
    params: { path: { citation_id: citationId } },
    signal,
  });
  const envelope: unknown = response.data;
  if (
    !isRecord(envelope) ||
    envelope.schema_version !== 1 ||
    envelope.workspace_id !== scope.workspaceId ||
    envelope.environment_id !== scope.environmentId
  ) {
    throw unavailableError();
  }

  const result = envelope.data;
  if (
    !isRecord(result) ||
    result.schema_version !== 'citation-inspection-result:v1' ||
    (result.status !== 'available' && result.status !== 'unavailable') ||
    (result.status === 'available' && result.reason !== 'available') ||
    (result.status === 'unavailable' && result.reason !== 'citation_unavailable')
  ) {
    throw unavailableError();
  }
  if (result.status === 'unavailable') return { status: 'unavailable' };

  const evidence: unknown = result.evidence;
  if (!matchesCurrentEvidence(evidence, scope, lineage, citationId)) {
    throw unavailableError();
  }
  return { status: 'available', evidence };
}

function matchesCurrentEvidence(
  value: unknown,
  scope: Scope,
  lineage: AcceptedAnswerLineage,
  citationId: string,
): value is CitationInspectionEvidence {
  if (!isRecord(value) || !isRecord(value.context_bundle) || !isRecord(value.evidence_reference)) {
    return false;
  }
  const context = value.context_bundle;
  const reference = value.evidence_reference;
  return (
    value.schema_version === 'citation-inspection-evidence:v1' &&
    value.citation_id === citationId &&
    value.answer_id === lineage.answerId &&
    value.workspace_id === scope.workspaceId &&
    value.environment_id === scope.environmentId &&
    value.historical === false &&
    typeof value.excerpt === 'string' &&
    value.excerpt.length > 0 &&
    typeof value.locator_schema === 'string' &&
    typeof value.locator_reference === 'string' &&
    typeof value.source_object_id === 'string' &&
    typeof value.source_revision_id === 'string' &&
    value.source_object_id === reference.source_object_id &&
    value.source_revision_id === reference.source_revision_id &&
    context.schema_version === 'context-bundle-lineage-ref:v1' &&
    context.workspace_id === scope.workspaceId &&
    context.environment_id === scope.environmentId &&
    context.run_id === lineage.runId &&
    context.attempt_id === lineage.attemptId &&
    (reference.schema_version === 'context-evidence-reference:v1' ||
      reference.schema_version === 'context-evidence-reference:v2') &&
    reference.workspace_id === scope.workspaceId &&
    reference.environment_id === scope.environmentId
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function unavailableError(): Error {
  return new Error('The citation is unavailable.');
}
