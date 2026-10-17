import type { components } from '../../../api/generated/schema';

import type { ApiScope, ScopedApiClient } from '../../api/client';

export type QARunOutcome = components['schemas']['AnswerQuestionOutcome'];
export type QARunSummary = components['schemas']['QARunSummaryResponse'];
export type QARunDetail = components['schemas']['QARunDetailResponse'];
export type QARunCollection =
  components['schemas']['ApiCollectionEnvelope_QARunSummaryResponse_'];

export const QA_RUN_OUTCOMES = [
  'answered',
  'clarification_required',
  'abstained',
  'denied',
  'failed',
  'cancelled',
] as const satisfies readonly QARunOutcome[];

export interface QARunQuery {
  cursor?: string;
  subjectId?: string;
  outcome?: QARunOutcome;
  createdAfter?: string;
  createdBefore?: string;
}

const PAGE_LIMIT = 25;
const MAX_CURSOR_LENGTH = 768;
const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function parseQARunQuery(search: string): QARunQuery {
  const parameters = new URLSearchParams(search);
  const query: QARunQuery = {};

  const cursor = parameters.get('cursor');
  if (cursor && cursor.length <= MAX_CURSOR_LENGTH) query.cursor = cursor;

  const subjectId = parameters.get('subject_id');
  if (subjectId && isValidQARunSubjectId(subjectId)) query.subjectId = subjectId;

  const outcome = parameters.get('outcome');
  if (outcome && isQARunOutcome(outcome)) query.outcome = outcome;

  const createdAfter = parameters.get('created_after');
  if (createdAfter && isDateTime(createdAfter)) query.createdAfter = createdAfter;

  const createdBefore = parameters.get('created_before');
  if (createdBefore && isDateTime(createdBefore)) query.createdBefore = createdBefore;

  if (
    query.createdAfter &&
    query.createdBefore &&
    Date.parse(query.createdAfter) > Date.parse(query.createdBefore)
  ) {
    delete query.createdAfter;
    delete query.createdBefore;
  }

  return query;
}

export function updateQARunQuery(search: string, next: QARunQuery): string {
  const parameters = new URLSearchParams(search);
  for (const key of ['cursor', 'subject_id', 'outcome', 'created_after', 'created_before']) {
    parameters.delete(key);
  }

  if (next.cursor && next.cursor.length <= MAX_CURSOR_LENGTH) parameters.set('cursor', next.cursor);
  if (next.subjectId && isValidQARunSubjectId(next.subjectId)) parameters.set('subject_id', next.subjectId);
  if (next.outcome && isQARunOutcome(next.outcome)) parameters.set('outcome', next.outcome);
  if (next.createdAfter && isDateTime(next.createdAfter)) {
    parameters.set('created_after', next.createdAfter);
  }
  if (next.createdBefore && isDateTime(next.createdBefore)) {
    parameters.set('created_before', next.createdBefore);
  }
  return parameters.toString();
}

export async function loadQARuns(
  client: ScopedApiClient,
  query: QARunQuery,
  signal: AbortSignal,
  expectedScope?: ApiScope,
): Promise<QARunCollection> {
  const result = await client.GET('/api/v1/qa/runs', {
    params: { query: toApiQuery(query) },
    signal,
  });
  if (!result.data) throw new Error('The Q&A run history response is unavailable.');
  if (expectedScope && !isInScope(result.data, expectedScope)) {
    throw new Error('The Q&A run history response is outside the selected scope.');
  }
  return result.data;
}

export async function loadQARunDetail(
  client: ScopedApiClient,
  runId: string,
  signal: AbortSignal,
  expectedScope?: ApiScope,
): Promise<QARunDetail> {
  if (!isValidQARunId(runId)) {
    throw new Error('The Q&A run identity is invalid.');
  }
  const result = await client.GET('/api/v1/qa/runs/{run_id}', {
    params: { path: { run_id: runId } },
    signal,
  });
  if (!result.data) throw new Error('The Q&A run detail response is unavailable.');
  const detail = result.data.data;
  if (
    expectedScope &&
    (!isEnvelopeInScope(result.data, expectedScope) || !isDetailInScope(detail, runId, expectedScope))
  ) {
    throw new Error('The Q&A run detail response is outside the selected scope.');
  }
  if (detail.run_id !== runId) {
    throw new Error('The Q&A run detail response does not match the selected run.');
  }
  return detail;
}

function toApiQuery(query: QARunQuery) {
  return {
    limit: PAGE_LIMIT,
    ...(query.cursor ? { cursor: query.cursor } : {}),
    ...(query.subjectId ? { subject_id: query.subjectId } : {}),
    ...(query.outcome ? { outcome: query.outcome } : {}),
    ...(query.createdAfter ? { created_after: query.createdAfter } : {}),
    ...(query.createdBefore ? { created_before: query.createdBefore } : {}),
  };
}

function isQARunOutcome(value: string): value is QARunOutcome {
  return (QA_RUN_OUTCOMES as readonly string[]).includes(value);
}

export function isValidQARunSubjectId(value: string): boolean {
  return isValidQARunId(value);
}

export function isValidQARunId(value: string): boolean {
  return uuidPattern.test(value);
}

function isDateTime(value: string): boolean {
  return value.length <= 64 && !Number.isNaN(Date.parse(value));
}

function isInScope(collection: QARunCollection, scope: ApiScope): boolean {
  return (
    isEnvelopeInScope(collection, scope) &&
    collection.items.every(
      (run) => run.workspace_id === scope.workspaceId && run.environment_id === scope.environmentId,
    )
  );
}

function isEnvelopeInScope(
  value: { workspace_id: string; environment_id: string },
  scope: ApiScope,
): boolean {
  return value.workspace_id === scope.workspaceId && value.environment_id === scope.environmentId;
}

function isDetailInScope(
  detail: QARunDetail,
  runId: string,
  scope: ApiScope,
): boolean {
  return (
    detail.run_id === runId &&
    detail.workspace_id === scope.workspaceId &&
    detail.environment_id === scope.environmentId &&
    detail.attempts.every((attempt) => attempt.run_id === runId) &&
    detail.accepted_evidence.every(
      (evidence) =>
        evidence.workspace_id === scope.workspaceId &&
        evidence.environment_id === scope.environmentId,
    )
  );
}
