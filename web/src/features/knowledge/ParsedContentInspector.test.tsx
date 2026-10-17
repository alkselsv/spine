import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { AuthProvider } from '../../app/auth';
import { ScopeContext, type Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { DocumentDetailPage } from './DocumentDetailPage';

const scope: Scope = {
  workspaceId: '10000000-0000-4000-8000-000000000002',
  environmentId: '20000000-0000-4000-8000-000000000002',
};
const sourceObjectId = '30000000-0000-4000-8000-000000000001';
const revisionId = '40000000-0000-4000-8000-000000000001';
const processingReceiptId = '50000000-0000-4000-8000-000000000003';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Knowledge parsed content inspection', () => {
  it('test_authorized_revision_renders_locator_variants_and_request_scope', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope()))
      .mockResolvedValueOnce(Response.json(makeInspectionEnvelope({
        items: [
          makeSegment({
            text: 'PDF evidence text',
            locator: {
              end_codepoint: 16,
              kind: 'page_text',
              page_number: 3,
              start_codepoint: 0,
            },
          }),
          makeSegment({
            segment_id: '80000000-0000-4000-8000-000000000002',
            text: 'Spreadsheet evidence text',
            locator: {
              cell_range: 'B4:C4',
              kind: 'xlsx_cell',
              row: 4,
              schema_version: 'locator:xlsx-cell:v1',
              sheet: 'Prices',
            },
          }),
          makeSegment({
            segment_id: '80000000-0000-4000-8000-000000000003',
            text: 'Line evidence text',
            locator: {
              end_codepoint: 10,
              kind: 'physical_line',
              line_number: 12,
              start_codepoint: 2,
            },
          }),
          makeSegment({
            segment_id: '80000000-0000-4000-8000-000000000004',
            text: 'DOCX evidence text',
            locator: {
              block_path: [{ index: 1, kind: 'paragraph' }],
              end_codepoint: 9,
              kind: 'ooxml_text',
              start_codepoint: 0,
            },
          }),
        ],
      })));
    vi.stubGlobal('fetch', fetch);

    renderDetail(
      `/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=content&revision=${revisionId}`,
    );

    expect(await screen.findByRole('heading', { name: ru.documents.contentInspectionTitle }, { timeout: 5_000 })).toBeVisible();
    expect(await screen.findByText('PDF evidence text')).toBeVisible();
    expect(await screen.findByText('Spreadsheet evidence text')).toBeVisible();
    expect(await screen.findByText('Line evidence text')).toBeVisible();
    expect(await screen.findByText('DOCX evidence text')).toBeVisible();
    expect(screen.getByText(new RegExp(`${ru.documents.locatorPage} 3`))).toBeVisible();
    expect(screen.getByText(new RegExp(`${ru.documents.locatorSheet} Prices.*${ru.documents.locatorRow} 4.*${ru.documents.locatorCells} B4:C4`))).toBeVisible();
    expect(screen.getByText(new RegExp(`${ru.documents.locatorLine} 12`))).toBeVisible();
    expect(screen.getByText(new RegExp(`${ru.documents.locatorPath} Paragraph 2`))).toBeVisible();
    expect(screen.getAllByText('parser-v1')).toHaveLength(2);
    expect(screen.getByText('locator-v1')).toBeVisible();

    const request = fetch.mock.calls
      .map(([input]) => input)
      .find((input) => input instanceof Request && new URL(input.url).pathname.endsWith('/parsed-content')) as Request;
    expect(request).toBeInstanceOf(Request);
    const url = new URL(request.url);
    expect(url.pathname).toBe(
      `/api/v1/knowledge/documents/${sourceObjectId}/revisions/${revisionId}/parsed-content`,
    );
    expect(url.searchParams.get('processing_receipt_id')).toBe(processingReceiptId);
    expect(request.headers.get('X-Workspace-ID')).toBe(scope.workspaceId);
    expect(request.headers.get('X-Environment-ID')).toBe(scope.environmentId);
  });

  it('test_denied_inspection_hides_payload_and_exposes_permission_state', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope()))
      .mockResolvedValueOnce(Response.json({
        code: 'spine.authorization.denied',
        retryability: 'never',
        safe_message: 'Not authorized.',
        schema_version: 1,
        trace_id: '70000000-0000-4000-8000-000000000001',
        protected_text: 'secret content must never be rendered',
      }, { status: 403 })));

    renderDetail(contentUrl());

    expect(await screen.findByRole('alert')).toHaveTextContent(ru.errors.authorization.title);
    expect(globalThis.document.body).not.toHaveTextContent('secret content must never be rendered');
    expect(globalThis.document.body).not.toHaveTextContent('parser-v1');
  });

  it.each([
    ['revision_not_current', 'Р”Р°РЅРЅС‹Рµ СѓСЃС‚Р°СЂРµР»Рё'],
    ['tombstone', 'РЎРѕРґРµСЂР¶РёРјРѕРµ РЅРµРґРѕСЃС‚СѓРїРЅРѕ'],
    ['processing_incomplete', 'РџРѕРєР°Р·Р°ЅС‹ РЅРµ РІСЃРµ РґР°РЅРЅС‹Рµ'],
  ] as const)('test_%s_inspection_is_safe', async (status, _expected) => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope({
        lifecycle: status === 'revision_not_current' ? 'superseded' : 'current',
        kind: status === 'tombstone' ? 'tombstone' : 'content',
      })))
      .mockResolvedValueOnce(Response.json(makeInspectionEnvelope({
        status: status === 'tombstone' ? 'content_unavailable' : status,
      })));
    vi.stubGlobal('fetch', fetch);

    renderDetail(contentUrl());

    const expectedCopy = status === 'revision_not_current'
      ? ru.status.stale.title
      : status === 'processing_incomplete'
        ? ru.status.partial.title
        : ru.documents.contentUnavailable;
    expect(await screen.findByText(expectedCopy)).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent('secret content must never be rendered');
  });

  it('test_malformed_locator_does_not_render_protected_segment_text', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope()))
      .mockResolvedValueOnce(Response.json(makeInspectionEnvelope({
        items: [makeSegment({
          text: 'malformed locator secret',
          locator: { kind: 'page_text', page_number: -1 } as never,
        })],
      }))));

    renderDetail(contentUrl());

    expect(await screen.findByText(ru.status.validationFailed.title)).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent('malformed locator secret');
  });

  it('test_mismatched_revision_lineage_does_not_render_protected_segment_text', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json(makeDocumentEnvelope()))
      .mockResolvedValueOnce(Response.json(makeRevisionEnvelope()))
      .mockResolvedValueOnce(Response.json(makeInspectionEnvelope({
        source_revision_digest: 'mismatched-revision-digest',
        items: [makeSegment({ text: 'mismatched lineage secret' })],
      }))));

    renderDetail(contentUrl());

    expect(await screen.findByText(ru.status.validationFailed.title)).toBeVisible();
    expect(globalThis.document.body).not.toHaveTextContent('mismatched lineage secret');
  });
});

function renderDetail(initialEntry: string) {
  const router = createMemoryRouter([
    { path: '/knowledge/documents/:documentId', element: <DocumentDetailPage /> },
  ], { initialEntries: [initialEntry] });
  render(
    <AuthProvider value={{ status: 'authenticated', role: 'administrator', getBearerCredential: () => 'test-token' }}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ScopeContext.Provider value={{
          scope,
          queryKey: (resource) => ['spine', resource, scope.workspaceId, scope.environmentId],
        }}>
          <RouterProvider router={router} />
        </ScopeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>,
  );
}

function contentUrl() {
  return `/knowledge/documents/${sourceObjectId}?workspace=${scope.workspaceId}&environment=${scope.environmentId}&tab=content&revision=${revisionId}`;
}

function makeDocumentEnvelope() {
  return {
    data: {
      created_at: '2026-10-15T09:00:00Z',
      current_revision: {
        accepted_at: '2026-10-15T10:01:00Z',
        accepted_generation: 7,
        admission_status: 'admitted',
        kind: 'content',
        lifecycle: 'current',
        observed_at: '2026-10-15T10:00:00Z',
        predecessor_revision_id: null,
        processing_references: [{
          coverage: 'complete',
          ingestion_item_id: '50000000-0000-4000-8000-000000000002',
          ingestion_run_id: '50000000-0000-4000-8000-000000000001',
          processing_receipt_id: processingReceiptId,
          state: 'processed',
        }],
        projection_references: [],
        schema_version: 1,
        source_revision_id: revisionId,
        superseded_by_revision_id: null,
      },
      operational_metadata: null,
      publication: null,
      schema_version: 1,
      source_kind: 'document',
      source_object_id: sourceObjectId,
    } satisfies components['schemas']['DocumentSnapshot'],
    environment_id: scope.environmentId,
    schema_version: 1,
    trace_id: '70000000-0000-4000-8000-000000000001',
    workspace_id: scope.workspaceId,
  };
}

function makeRevisionEnvelope(overrides: Partial<components['schemas']['RevisionSnapshot']> = {}) {
  return {
    environment_id: scope.environmentId,
    items: [{
      accepted_at: '2026-10-15T10:01:00Z',
      accepted_generation: 7,
      admission_status: 'admitted',
      content: null,
      deletion_reason: null,
      kind: 'content',
      lifecycle: 'current',
      observed_at: '2026-10-15T10:00:00Z',
      predecessor_revision_id: null,
      processing_references: [{
        coverage: 'complete',
        ingestion_item_id: '50000000-0000-4000-8000-000000000002',
        ingestion_run_id: '50000000-0000-4000-8000-000000000001',
        processing_receipt_id: processingReceiptId,
        state: 'processed',
      }],
      projection_references: [],
      schema_version: 1,
      source_revision_id: revisionId,
      superseded_by_revision_id: null,
      ...overrides,
    } satisfies components['schemas']['RevisionSnapshot']],
    limit: 25,
    next_cursor: null,
    schema_version: 1,
    trace_id: '70000000-0000-4000-8000-000000000001',
    workspace_id: scope.workspaceId,
  };
}

function makeInspectionEnvelope(
  overrides: Partial<components['schemas']['ParsedContentInspectionResponse']> = {},
) {
  return {
    data: {
      artifact_digest: 'artifact-digest',
      artifact_id: '80000000-0000-4000-8000-000000000010',
      items: [],
      locator_contract_version: 'locator-v1',
      next_cursor: null,
      original_sha256: 'original-sha256',
      parser_config_reference: 'parser-config-v1',
      parser_name: 'parser-v1',
      parser_version: 'parser-v1',
      processing_receipt_id: processingReceiptId,
      schema_version: 'knowledge-parsed-content-inspection:v1',
      source_object_id: sourceObjectId,
      source_revision_digest: 'revision-digest',
      source_revision_id: revisionId,
      status: 'ready',
      ...overrides,
    },
    environment_id: scope.environmentId,
    schema_version: 1,
    trace_id: '70000000-0000-4000-8000-000000000001',
    workspace_id: scope.workspaceId,
  } satisfies components['schemas']['ApiResponseEnvelope_ParsedContentInspectionResponse_'];
}

function makeSegment(
  overrides: Partial<components['schemas']['ParsedContentSegmentResponse']> = {},
) {
  return {
    heading_path: ['Section'],
    locator: {
      end_codepoint: 5,
      kind: 'physical_line',
      line_number: 1,
      start_codepoint: 0,
    },
    segment_digest: 'segment-digest',
    segment_id: '80000000-0000-4000-8000-000000000001',
    source_object_id: sourceObjectId,
    source_revision_digest: 'revision-digest',
    source_revision_id: revisionId,
    text: 'synthetic segment',
    text_digest: 'text-digest',
    ...overrides,
  } satisfies components['schemas']['ParsedContentSegmentResponse'];
}
