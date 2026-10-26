import type { BrowserQualificationFixture } from './fixture';
import {
  INITIAL_MESSAGES,
  INITIAL_SESSIONS,
  plainText,
} from '../ported/spine/chat/spineChatData.js';
import { runDetail } from '../ported/spine/runs/qaRunData.js';
import { INITIAL_DOCUMENTS } from '../ported/spine/spineData.js';

const TRACE_ID = '50000000-0000-4000-8000-000000000001';
const SUBJECT_ID = '50000000-0000-4000-8000-000000000002';
interface PrototypeSession {
  session_id: string;
  workspace_id: string;
  environment_id: string;
  status: 'active' | 'archived';
  version: number;
  created_at: string;
  updated_at: string;
}

interface PrototypeCitation {
  citation_id: string;
  source_object_id: string;
  source_revision_id: string;
  revision_number: number;
  source_kind: string;
  locator: { kind: string; value: string };
  excerpt: string;
}

interface PrototypePart {
  text: string;
  claim: string;
}

interface PrototypeMessage {
  message_id: string;
  session_id: string;
  ordinal: number;
  created_at: string;
  question: string;
  state: 'terminal';
  terminal_outcome: 'answered' | 'clarification_required' | 'abstained' | 'denied' | 'failed' | 'cancelled';
  disclosure_eligibility: 'eligible' | 'ineligible';
  run_id: string;
  trace_id: string;
  answer_id: string | null;
  version: number;
  clarification_prompt: string | null;
  citations: PrototypeCitation[];
  claims: Record<string, {
    kind: 'factual' | 'derived';
    rel: Array<{ kind: string; cites?: number[]; related?: string }>;
  }>;
  blocks: Array<
    | { type: 'p'; parts: Array<string | PrototypePart> }
    | { type: 'ul'; items: Array<Array<string | PrototypePart>> }
  >;
  completeness: Record<string, unknown> | null;
  run: {
    attempt_id: string;
    attempt_number: number;
    duration_ms: number;
    context: number;
  };
}

const PROTOTYPE_SESSIONS = (INITIAL_SESSIONS as PrototypeSession[])
  .filter(session => session.workspace_id === 'primary' && session.environment_id === 'local');
const PROTOTYPE_MESSAGES = INITIAL_MESSAGES as Record<string, PrototypeMessage[]>;

let installed = false;

export function installBrowserQualificationFixtures(
  fixture: BrowserQualificationFixture | null,
  runtimeAuthenticated: boolean,
): void {
  if (!fixture || fixture === 'auth-denied' || !runtimeAuthenticated || installed) return;
  globalThis.fetch = createBrowserQualificationFetch(globalThis.fetch.bind(globalThis));
  installed = true;
}

export function createBrowserQualificationFetch(
  fallback: typeof fetch,
): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/v1/')) {
      return fallback(input, init);
    }

    const scope = {
      workspaceId: request.headers.get('X-Workspace-ID') ?? '',
      environmentId: request.headers.get('X-Environment-ID') ?? '',
    };
    if (!scope.workspaceId || !scope.environmentId) {
      return json({ code: 'spine.request.validation', safe_message: 'Fixture scope is missing.' }, 422);
    }

    if (request.method === 'GET' && url.pathname === '/api/v1/health/ready') {
      return json({ service: 'spine', status: 'ready', code: 'local.ready' });
    }
    if (request.method === 'GET' && url.pathname === '/api/v1/documents') {
      return json(collection(scope, fixtureDocuments(scope)));
    }
    const revisionsMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/revisions$/);
    if (request.method === 'GET' && revisionsMatch) {
      const document = fixtureDocuments(scope).find(item => item.source_object_id === revisionsMatch[1]);
      return document
        ? json(collection(scope, document.revisions))
        : notFound();
    }
    const documentMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)$/);
    if (request.method === 'GET' && documentMatch) {
      const document = fixtureDocuments(scope).find(item => item.source_object_id === documentMatch[1]);
      return document ? json(envelope(scope, document)) : notFound();
    }
    if (request.method === 'GET' && url.pathname === '/api/v1/qa/sessions') {
      return json(collection(scope, fixtureSessions(scope)));
    }
    const messageMatch = url.pathname.match(
      /^\/api\/v1\/qa\/sessions\/([^/]+)\/messages\/([^/]+)$/,
    );
    if (request.method === 'GET' && messageMatch) {
      const messages = await fixtureMessages(scope, messageMatch[1]!);
      const message = messages.find(item => item.message_id === messageMatch[2]);
      return message ? json(envelope(scope, message)) : notFound();
    }
    const messagesMatch = url.pathname.match(/^\/api\/v1\/qa\/sessions\/([^/]+)\/messages$/);
    if (request.method === 'GET' && messagesMatch) {
      return json(collection(scope, await fixtureMessages(scope, messagesMatch[1]!)));
    }
    const sessionMatch = url.pathname.match(/^\/api\/v1\/qa\/sessions\/([^/]+)$/);
    if (request.method === 'GET' && sessionMatch) {
      const session = fixtureSessions(scope).find(item => item.session_id === sessionMatch[1]);
      return session ? json(envelope(scope, session)) : notFound();
    }
    if (request.method === 'GET' && url.pathname === '/api/v1/qa/runs') {
      return json(collection(scope, filterFixtureRuns(fixtureRuns(scope), url.searchParams)));
    }
    const runMatch = url.pathname.match(/^\/api\/v1\/qa\/runs\/([^/]+)$/);
    if (request.method === 'GET' && runMatch) {
      const detail = fixtureRunDetail(scope, runMatch[1]!);
      return detail ? json(envelope(scope, detail)) : notFound();
    }
    const citationMatch = url.pathname.match(/^\/api\/v1\/qa\/citations\/([^/]+)$/);
    if (request.method === 'GET' && citationMatch) {
      const inspection = fixtureCitationInspection(scope, citationMatch[1]!);
      return inspection ? json(envelope(scope, inspection)) : notFound();
    }

    return json(
      {
        schema_version: 1,
        code: 'spine.fixture.unsupported',
        retryability: 'never',
        safe_message: 'Эта операция недоступна в демонстрационном режиме.',
        trace_id: TRACE_ID,
      },
      503,
    );
  };
}

interface FixtureScope {
  workspaceId: string;
  environmentId: string;
}

function fixtureDocuments(scope: FixtureScope) {
  return INITIAL_DOCUMENTS
    .filter(document => document.scope === 'primary/local')
    .map((document, documentIndex) => {
      const revisions = document.revisions.map((revision: {
        source_revision_id: string;
        lifecycle: string;
        observed_at: string;
        reason?: string;
      }, revisionIndex: number) => ({
        schema_version: 1,
        ...revision,
        kind: revision.lifecycle === 'tombstoned' ? 'tombstone' : 'content',
        accepted_at: revisionIndex === 0 ? document.current_revision.accepted_at : revision.observed_at,
        accepted_generation: revisionIndex === 0 ? document.current_revision.accepted_generation : null,
        admission_status: revisionIndex === 0 ? document.current_revision.admission_status : 'accepted',
        deletion_reason: revision.reason ?? null,
        predecessor_revision_id: document.revisions[revisionIndex + 1]?.source_revision_id ?? null,
        superseded_by_revision_id: revisionIndex > 0 ? document.revisions[revisionIndex - 1]?.source_revision_id ?? null : null,
        processing_references: [],
        projection_references: [],
        content: null,
      }));
      const currentRevision = {
        ...revisions[0],
        ...document.current_revision,
      };
      const published = Boolean(document.publication);
      return {
        schema_version: 1,
        source_object_id: document.source_object_id,
        source_kind: document.source_kind,
        created_at: document.created_at,
        current_revision: currentRevision,
        revisions,
        publication: document.publication ? {
          ...document.publication,
          projection_kind: 'qa',
          snapshot_id: stableUuid(document.source_object_id, 400 + documentIndex),
          drift_count: document.publication.freshness === 'stale' ? 2 : 0,
          manifest_complete: true,
          source_covered: document.coverage !== 'failed',
        } : {
          projection_kind: 'qa',
          status: 'unpublished',
          snapshot_id: null,
          freshness: null,
          health: null,
          drift_count: 0,
          manifest_complete: null,
          source_covered: null,
        },
        operational_metadata: {
          schema_version: 1,
          source_object_id: document.source_object_id,
          source_revision_id: currentRevision.source_revision_id,
          ...document.operational_metadata,
          publication_status: document.operational_metadata.publication_status ?? (published ? 'published' : null),
        },
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
      };
    });
}

function fixtureSessions(scope: FixtureScope) {
  return PROTOTYPE_SESSIONS.map(session => ({
    schema_version: 1,
    session_id: session.session_id,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    subject_id: SUBJECT_ID,
    version: session.version,
    status: session.status,
    created_at: session.created_at,
    updated_at: session.updated_at,
  }));
}

async function fixtureMessages(scope: FixtureScope, sessionId: string) {
  return Promise.all((PROTOTYPE_MESSAGES[sessionId] ?? []).map(async message => {
    const accepted = await acceptedPresentation(message);
    const answer = message.disclosure_eligibility === 'ineligible'
      ? null
      : message.terminal_outcome === 'answered'
        ? accepted?.answer ?? null
        : message.terminal_outcome === 'clarification_required'
          ? message.clarification_prompt
          : null;
    return {
      schema_version: 1,
      message_id: message.message_id,
      session_id: message.session_id,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      version: message.version,
      ordinal: message.ordinal,
      trace_id: stableUuid(message.message_id, 20),
      run_id: message.run_id,
      state: message.state,
      terminal_outcome: message.terminal_outcome,
      cancelled: message.terminal_outcome === 'cancelled',
      disclosure_eligibility: message.disclosure_eligibility,
      question: message.disclosure_eligibility === 'ineligible' ? null : message.question,
      answer,
      accepted_presentation: accepted?.presentation ?? null,
      completeness: message.completeness ? fixtureCompleteness(message) : null,
      created_at: message.created_at,
      updated_at: new Date(Date.parse(message.created_at) + message.run.duration_ms).toISOString(),
    };
  }));
}

function fixtureRuns(scope: FixtureScope) {
  return PROTOTYPE_SESSIONS.flatMap((session, sessionIndex) =>
    (PROTOTYPE_MESSAGES[session.session_id] ?? []).map(message => ({
      schema_version: 1,
      run_id: message.run_id,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      trace_id: stableUuid(message.message_id, 20),
      subject_id: stableUuid(SUBJECT_ID, sessionIndex === 1 ? 2 : 1),
      subject_kind: 'human',
      session_id: message.session_id,
      message_id: message.message_id,
      state: 'completed',
      outcome: message.terminal_outcome,
      attempt_count: message.run.attempt_number,
      final_attempt_id: message.run.attempt_id,
      created_at: message.created_at,
      completed_at: new Date(Date.parse(message.created_at) + message.run.duration_ms).toISOString(),
    })),
  ).sort((left, right) => right.created_at.localeCompare(left.created_at));
}

function filterFixtureRuns<T extends {
  outcome: string | null;
  subject_id: string;
  created_at: string;
}>(runs: T[], search: URLSearchParams): T[] {
  const outcome = search.get('outcome');
  const subjectId = search.get('subject_id');
  const createdAfter = search.get('created_after');
  const createdBefore = search.get('created_before');
  return runs.filter(run =>
    (!outcome || run.outcome === outcome)
    && (!subjectId || run.subject_id === subjectId)
    && (!createdAfter || Date.parse(run.created_at) >= Date.parse(createdAfter))
    && (!createdBefore || Date.parse(run.created_at) <= Date.parse(createdBefore))
  );
}

function fixtureRunDetail(scope: FixtureScope, runId: string) {
  const located = findPrototypeMessage(message => message.run_id === runId);
  if (!located) return null;
  const detail = runDetail(located.message, located.session);
  return {
    ...detail,
    schema_version: 1,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    trace_id: stableUuid(located.message.message_id, 20),
    input_artifact_ids: [],
    output_artifact_ids: [],
    attempts: detail.attempts.map((attempt: Record<string, unknown>) => ({
      ...attempt,
      schema_version: 'qa-run-attempt:v1',
      run_id: runId,
      input_artifact_ids: [],
      output_artifact_ids: [],
      agent_result_id: null,
      context_bundle_digest: null,
      context_bundle_id: null,
      context_bundle_version: null,
    })),
    accepted_evidence: detail.accepted_evidence.map((evidence: Record<string, unknown>, index: number) => ({
      ...evidence,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      activation_generation: 7,
      locator_schema: 'fixture-locator:v1',
      projection_snapshot_id: stableUuid(runId, 70 + index),
    })),
  };
}

function fixtureCitationInspection(scope: FixtureScope, citationId: string) {
  const located = findPrototypeMessage(message => message.citations.some(citation => citation.citation_id === citationId));
  if (!located || !located.message.answer_id) return null;
  const citationIndex = located.message.citations.findIndex(item => item.citation_id === citationId);
  const citation = located.message.citations[citationIndex];
  if (!citation) return null;
  const sourceObjectId = citation.source_object_id;
  const sourceRevisionId = citation.source_revision_id;
  const reference = {
    schema_version: 'context-evidence-reference:v1',
    status: 'valid',
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    source_object_id: sourceObjectId,
    source_revision_id: sourceRevisionId,
  };
  return {
    schema_version: 'citation-inspection-result:v1',
    status: 'available',
    reason: 'available',
    evidence: {
      schema_version: 'citation-inspection-evidence:v1',
      citation_id: citation.citation_id,
      answer_id: located.message.answer_id,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      historical: false,
      excerpt: citation.excerpt,
      locator_schema: `fixture:${citation.locator.kind}`,
      locator_reference: citation.locator.value,
      source_object_id: sourceObjectId,
      source_revision_id: sourceRevisionId,
      revision_number: citation.revision_number,
      context_bundle: {
        schema_version: 'context-bundle-lineage-ref:v1',
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        run_id: located.message.run_id,
        attempt_id: located.message.run.attempt_id,
      },
      evidence_reference: reference,
    },
  };
}

async function acceptedPresentation(message: PrototypeMessage) {
  if (
    message.terminal_outcome !== 'answered'
    || message.disclosure_eligibility === 'ineligible'
    || !message.answer_id
  ) return null;

  const answer = plainText(message) as string;
  const claimIds = new Map(
    Object.keys(message.claims).map((claimId, index) => [claimId, stableUuid(message.message_id, 100 + index)]),
  );
  const claims = Object.entries(message.claims).map(([claimId, claim], index) => {
    const claimText = findClaimText(message, claimId);
    const startOffset = claimText ? answer.indexOf(claimText) : 0;
    return {
      schema_version: 'accepted-answer-claim-presentation:v1',
      claim_id: claimIds.get(claimId)!,
      kind: claim.kind,
      start_offset: Math.max(0, startOffset),
      end_offset: Math.max(0, startOffset) + claimText.length,
      index,
    };
  });
  const relationships = Object.entries(message.claims).flatMap(([claimId, claim], claimIndex) =>
    claim.rel.map((relationship, relationshipIndex) => {
      const citationIds = (relationship.cites ?? [])
        .map(index => message.citations[index]?.citation_id)
        .filter((citationId): citationId is string => Boolean(citationId));
      return {
        schema_version: 'accepted-answer-relationship-presentation:v1',
        relationship_id: stableUuid(message.message_id, 200 + claimIndex * 10 + relationshipIndex),
        claim_id: claimIds.get(claimId)!,
        kind: citationIds.length > 1 && relationship.kind === 'corroborating'
          ? 'joint'
          : relationship.kind,
        citation_ids: citationIds,
        related_claim_id: relationship.related ? claimIds.get(relationship.related) ?? null : null,
      };
    }),
  );
  return {
    answer,
    presentation: {
      schema_version: 'accepted-answer-presentation:v1',
      answer_id: message.answer_id,
      answer_text_sha256: await sha256(answer),
      attempt_id: message.run.attempt_id,
      message_id: message.message_id,
      run_id: message.run_id,
      citation_ids: message.citations.map(citation => citation.citation_id),
      claims: claims.map(({ index: _index, ...claim }) => claim),
      relationships,
    },
  };
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function findClaimText(message: PrototypeMessage, claimId: string): string {
  for (const block of message.blocks) {
    const parts = block.type === 'ul' ? block.items.flat() : block.parts;
    for (const part of parts) {
      if (typeof part !== 'string' && part.claim === claimId) return part.text;
    }
  }
  return '';
}

function fixtureCompleteness(message: PrototypeMessage) {
  return {
    ...message.completeness,
    schema_version: 'answer-completeness-presentation:v1',
    target_scope_id: stableUuid(message.message_id, 301),
    target_scope_version: 'target-completeness-scope:v1',
    observed_coverage_id: stableUuid(message.message_id, 302),
    observed_coverage_version: 'observed-coverage:v1',
  };
}

function findPrototypeMessage(predicate: (message: PrototypeMessage) => boolean) {
  for (const session of PROTOTYPE_SESSIONS) {
    const message = (PROTOTYPE_MESSAGES[session.session_id] ?? []).find(predicate);
    if (message) return { message, session };
  }
  return null;
}

function stableUuid(source: string, salt: number): string {
  let hash = (2166136261 ^ salt) >>> 0;
  for (const character of source) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  const word = hash.toString(16).padStart(8, '0');
  const hex = `${word}${word}${word}${word}`;
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

function collection(scope: FixtureScope, items: readonly unknown[]) {
  return {
    schema_version: 1,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    trace_id: TRACE_ID,
    items,
    limit: 25,
    next_cursor: null,
  };
}

function envelope(scope: FixtureScope, data: unknown) {
  return {
    schema_version: 1,
    workspace_id: scope.workspaceId,
    environment_id: scope.environmentId,
    trace_id: TRACE_ID,
    data,
  };
}

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status });
}

function notFound(): Response {
  return json({
    schema_version: 1,
    code: 'spine.resource.not_found',
    retryability: 'never',
    safe_message: 'Fixture resource is unavailable.',
    trace_id: TRACE_ID,
  }, 404);
}
