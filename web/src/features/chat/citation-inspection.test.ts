import { afterEach, describe, expect, it, vi } from 'vitest';

import { createScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { inspectAcceptedCitation } from './citation-inspection';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000001',
  environmentId: '20000000-0000-4000-8000-000000000001',
};
const answerId = '80000000-0000-4000-8000-000000000001';
const runId = '70000000-0000-4000-8000-000000000001';
const attemptId = '90000000-0000-4000-8000-000000000001';
const citationId = 'c0000000-0000-4000-8000-000000000001';
const traceId = '60000000-0000-4000-8000-000000000001';

function clientFor(fetch: typeof globalThis.fetch) {
  return createScopedApiClient({
    baseUrl: 'https://spine.test',
    fetch,
    getBearerCredential: () => 'test-token',
    scope,
  });
}

function citationEvidence(overrides: Record<string, unknown> = {}) {
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
    excerpt: 'Синтетический фрагмент источника.',
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
    ...overrides,
  };
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('current-authorized citation inspection', () => {
  it('test_citation_inspection_uses_scoped_current_read_and_matches_accepted_lineage', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({
      schema_version: 1,
      trace_id: traceId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      data: {
        schema_version: 'citation-inspection-result:v1',
        status: 'available',
        reason: 'available',
        evidence: citationEvidence(),
      },
    }));

    const inspected = await inspectAcceptedCitation(
      clientFor(fetcher),
      scope,
      { answerId, attemptId, runId },
      citationId,
      new AbortController().signal,
    );

    expect(inspected.status).toBe('available');
    if (inspected.status !== 'available') throw new Error('Expected current citation evidence.');
    expect(inspected.evidence.excerpt).toBe('Синтетический фрагмент источника.');
    const request = fetcher.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe(`/api/v1/qa/citations/${citationId}`);
    expect(new URL(request.url).search).toBe('');
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
    expect(request.headers.get('Authorization')).toBe('Bearer test-token');
  });

  it('test_unavailable_citation_never_returns_excerpt_to_the_presentation', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({
      schema_version: 1,
      trace_id: traceId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      data: {
        schema_version: 'citation-inspection-result:v1',
        status: 'unavailable',
        reason: 'citation_unavailable',
        evidence: citationEvidence({ excerpt: 'Must not be released with an unavailable status.' }),
      },
    }));

    const inspected = await inspectAcceptedCitation(
      clientFor(fetcher),
      scope,
      { answerId, attemptId, runId },
      citationId,
      new AbortController().signal,
    );

    expect(inspected).toEqual({ status: 'unavailable' });
  });

  it('test_foreign_scope_or_stale_lineage_fails_closed_without_evidence', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({
      schema_version: 1,
      trace_id: traceId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      data: {
        schema_version: 'citation-inspection-result:v1',
        status: 'available',
        reason: 'available',
        evidence: citationEvidence({
          answer_id: '80000000-0000-4000-8000-000000000099',
          workspace_id: '10000000-0000-4000-8000-000000000099',
        }),
      },
    }));

    await expect(inspectAcceptedCitation(
      clientFor(fetcher),
      scope,
      { answerId, attemptId, runId },
      citationId,
      new AbortController().signal,
    )).rejects.toThrow('The citation is unavailable.');
  });

  it('test_historical_or_cross_attempt_evidence_fails_closed', async () => {
    const evidence = citationEvidence({
      historical: true,
      context_bundle: {
        ...citationEvidence().context_bundle,
        attempt_id: '90000000-0000-4000-8000-000000000099',
      },
    });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({
      schema_version: 1,
      trace_id: traceId,
      workspace_id: scope.workspaceId,
      environment_id: scope.environmentId,
      data: {
        schema_version: 'citation-inspection-result:v1',
        status: 'available',
        reason: 'available',
        evidence,
      },
    }));

    await expect(inspectAcceptedCitation(
      clientFor(fetcher),
      scope,
      { answerId, attemptId, runId },
      citationId,
      new AbortController().signal,
    )).rejects.toThrow('The citation is unavailable.');
  });

  it.each(['missing', 'stale', 'tombstoned', 'ambiguous', 'incomplete', 'inaccessible', 'fabricated', 'corrupt'] as const)(
    'test_%s_evidence_reference_fails_closed_without_excerpt',
    async (status) => {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({
        schema_version: 1,
        trace_id: traceId,
        workspace_id: scope.workspaceId,
        environment_id: scope.environmentId,
        data: {
          schema_version: 'citation-inspection-result:v1',
          status: 'available',
          reason: 'available',
          evidence: citationEvidence({
            evidence_reference: {
              ...citationEvidence().evidence_reference,
              status,
            },
          }),
        },
      }));

      await expect(inspectAcceptedCitation(
        clientFor(fetcher),
        scope,
        { answerId, attemptId, runId },
        citationId,
        new AbortController().signal,
      )).rejects.toThrow('The citation is unavailable.');
    },
  );
});
