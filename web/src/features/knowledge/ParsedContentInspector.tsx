import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { SpineApiError, createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { ru } from '../../i18n/resources';
import styles from './DocumentDetailPage.module.css';
import { loadRevisionHistory } from './revision-api';

type RevisionSnapshot = components['schemas']['RevisionSnapshot'];
type ParsedContentResponse = components['schemas']['ParsedContentInspectionResponse'];
type ParsedContentSegment = components['schemas']['ParsedContentSegmentResponse'];
type ParsedContentStatus = components['schemas']['ParsedContentInspectionStatus'];
type ParsedContentLocator = ParsedContentSegment['locator'];

const PARSED_CONTENT_PAGE_SIZE = 50;

export function ContentInspectionTab({
  authorizationKey,
  client,
  document,
  documentId,
  scope,
}: {
  authorizationKey: string;
  client: ReturnType<typeof createScopedApiClient>;
  document: components['schemas']['DocumentSnapshot'];
  documentId: string;
  scope: { environmentId: string; workspaceId: string };
}) {
  const [searchParams] = useSearchParams();
  const revisionCursor = searchParams.get('revision_cursor') ?? searchParams.get('cursor') ?? undefined;
  const selectedRevisionId = searchParams.get('revision') ?? document.current_revision?.source_revision_id ?? null;
  const revisionsQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadRevisionHistory(client, documentId, revisionCursor, signal),
      queryKey: [
        'spine',
        'document-content-revision',
        scope.workspaceId,
        scope.environmentId,
        documentId,
        revisionCursor,
      ],
    }),
    enabled: Boolean(selectedRevisionId),
  });

  if (!selectedRevisionId) {
    return <InspectionStatus status="content_unavailable" />;
  }
  if (revisionsQuery.isPending) return <StatusPanel state="loading" />;
  if (revisionsQuery.error) {
    return (
      <section className={styles.card} aria-labelledby="content-inspection-heading">
        <h3 id="content-inspection-heading">{ru.documents.contentInspectionTitle}</h3>
        <ApiErrorPanel error={revisionsQuery.error} />
      </section>
    );
  }
  if (!revisionsQuery.data) return <StatusPanel state="failed" />;

  const revision = revisionsQuery.data.items.find((item) => item.source_revision_id === selectedRevisionId);
  if (!revision) {
    return (
      <section className={styles.card} aria-labelledby="content-inspection-heading">
        <h3 id="content-inspection-heading">{ru.documents.contentInspectionTitle}</h3>
        <p className={styles.subviewNotice} role="status">{ru.documents.selectedRevisionUnavailable}</p>
      </section>
    );
  }

  return (
    <ParsedContentInspector
        authorizationKey={authorizationKey}
      client={client}
      documentId={documentId}
      revision={revision}
      searchParams={searchParams}
      scope={scope}
    />
  );
}

export function ParsedContentInspector({
  authorizationKey,
  client,
  documentId,
  revision,
  searchParams,
  scope,
}: {
  authorizationKey: string;
  client: ReturnType<typeof createScopedApiClient>;
  documentId: string;
  revision: RevisionSnapshot;
  searchParams: URLSearchParams;
  scope: { environmentId: string; workspaceId: string };
}) {
  const processingReceiptId = revision.processing_references.find(
    (reference) => reference.processing_receipt_id,
  )?.processing_receipt_id ?? null;
  const cursor = searchParams.get('content_cursor') ?? undefined;
  const inspectionQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadParsedContent(
        client,
        documentId,
        revision.source_revision_id,
        processingReceiptId ?? '',
        cursor,
        signal,
      ),
      queryKey: [
        'spine',
        'parsed-content',
        scope.workspaceId,
        scope.environmentId,
        authorizationKey,
        documentId,
        revision.source_revision_id,
        processingReceiptId,
        cursor,
      ],
      staleTime: 0,
    }),
    enabled: Boolean(processingReceiptId && revision.kind === 'content'),
    gcTime: 0,
    refetchOnMount: 'always',
  });

  return (
    <section className={styles.card} aria-labelledby="content-inspection-heading">
      <header className={styles.statusHeader}>
        <div>
          <h3 id="content-inspection-heading">{ru.documents.contentInspectionTitle}</h3>
          <p className={styles.cardDescription}>{ru.documents.contentInspectionDescription}</p>
        </div>
        <span className={styles.statusPill}>{revision.source_revision_id}</span>
      </header>

      {revision.kind !== 'content' || !processingReceiptId ? (
        <InspectionStatus status="content_unavailable" />
      ) : inspectionQuery.isPending || inspectionQuery.isFetching ? (
        <StatusPanel state="loading" />
      ) : inspectionQuery.error ? (
        <div>
          <ApiErrorPanel error={inspectionQuery.error} />
          {inspectionQuery.error instanceof SpineApiError && inspectionQuery.error.retryability === 'after_delay' ? (
            <button className={styles.buttonLink} onClick={() => void inspectionQuery.refetch()} type="button">
              {ru.documents.retry}
            </button>
          ) : null}
        </div>
      ) : !inspectionQuery.data ? (
        <StatusPanel state="failed" />
      ) : (
        <InspectionResult
          documentId={documentId}
          processingReceiptId={processingReceiptId}
          revisionId={revision.source_revision_id}
          response={inspectionQuery.data}
          searchParams={searchParams}
        />
      )}
    </section>
  );
}

export async function loadParsedContent(
  client: ReturnType<typeof createScopedApiClient>,
  sourceObjectId: string,
  sourceRevisionId: string,
  processingReceiptId: string,
  cursor: string | undefined,
  signal: AbortSignal,
): Promise<components['schemas']['ApiResponseEnvelope_ParsedContentInspectionResponse_']> {
  const result = await client.GET(
    '/api/v1/knowledge/documents/{source_object_id}/revisions/{source_revision_id}/parsed-content',
    {
      params: {
        path: { source_object_id: sourceObjectId, source_revision_id: sourceRevisionId },
        query: cursor
          ? { cursor, page_size: PARSED_CONTENT_PAGE_SIZE, processing_receipt_id: processingReceiptId }
          : { page_size: PARSED_CONTENT_PAGE_SIZE, processing_receipt_id: processingReceiptId },
      },
      signal,
    },
  );
  if (!result.data) throw new Error('The parsed content inspection response is unavailable.');
  return result.data;
}

function InspectionResult({
  documentId,
  processingReceiptId,
  revisionId,
  response,
  searchParams,
}: {
  documentId: string;
  processingReceiptId: string;
  revisionId: string;
  response: components['schemas']['ApiResponseEnvelope_ParsedContentInspectionResponse_'];
  searchParams: URLSearchParams;
}) {
  const data = response.data;
  if (data.status !== 'ready') return <InspectionStatus status={data.status} />;
  if (!isSafeInspectionResponse(data, documentId, revisionId, processingReceiptId)) {
    return <StatusPanel state="validationFailed" />;
  }
  if (data.items.length === 0) {
    return <StatusPanel state="empty" />;
  }

  return (
    <>
      <p className={styles.protectedNotice} role="note">{ru.documents.contentInspectionProtectedNotice}</p>
      <dl className={styles.details}>
        <DetailValue label={ru.documents.parser} value={data.parser_name ?? ru.documents.unknown} />
        <DetailValue label={ru.documents.parserVersion} value={data.parser_version ?? ru.documents.unknown} />
        <DetailValue label={ru.documents.locatorContractVersion} value={data.locator_contract_version ?? ru.documents.unknown} />
        <DetailValue label={ru.documents.sourceRevision} value={revisionId} code />
      </dl>
      <ol className={styles.contentSegments} aria-label={ru.documents.contentSegments}>
        {data.items.map((segment, index) => (
          <li className={styles.contentSegment} key={segment.segment_id}>
            <header className={styles.segmentHeader}>
              <strong>{ru.documents.contentSegment} {index + 1}</strong>
              <span className={styles.locator}>{formatLocator(segment.locator)}</span>
            </header>
            {segment.heading_path.length ? (
              <p className={styles.headingPath}>{segment.heading_path.join(' / ')}</p>
            ) : null}
            <pre className={styles.segmentText}>{segment.text}</pre>
          </li>
        ))}
      </ol>
      {data.next_cursor ? (
        <Link className={styles.buttonLink} to={{
          pathname: `/knowledge/documents/${documentId}`,
          search: `?${nextContentPageSearch(searchParams, data.next_cursor, revisionId)}`,
        }}>
          {ru.documents.nextContentPage}
        </Link>
      ) : null}
    </>
  );
}

function InspectionStatus({ status }: { status: ParsedContentStatus }) {
  if (status === 'revision_not_current') return <StatusPanel state="stale" />;
  if (status === 'processing_incomplete' || status === 'artifact_unavailable') {
    return <StatusPanel state="partial" />;
  }
  if (status === 'locator_invalid' || status === 'integrity_mismatch' || status === 'limit_exceeded') {
    return <StatusPanel state="validationFailed" />;
  }
  if (status === 'service_unavailable') return <StatusPanel state="degraded" />;
  return <p className={styles.subviewNotice} role="status">{ru.documents.contentUnavailable}</p>;
}

function isSafeInspectionResponse(
  response: ParsedContentResponse,
  documentId: string,
  revisionId: string,
  processingReceiptId: string,
): response is ParsedContentResponse & { items: ParsedContentSegment[] } {
  if (
    response.source_object_id !== documentId ||
    response.source_revision_id !== revisionId ||
    response.processing_receipt_id !== processingReceiptId ||
    typeof response.source_revision_digest !== 'string' ||
    response.source_revision_digest.length === 0 ||
    !Array.isArray(response.items)
  ) return false;
  return response.items.every((segment) =>
    segment.source_object_id === documentId &&
    segment.source_revision_id === revisionId &&
    segment.source_revision_digest === response.source_revision_digest &&
    typeof segment.text === 'string' &&
    segment.text.length > 0 &&
    Array.isArray(segment.heading_path) &&
    segment.heading_path.every((heading) => typeof heading === 'string') &&
    isValidLocator(segment.locator),
  );
}

function isValidLocator(locator: unknown): locator is ParsedContentLocator {
  if (!locator || typeof locator !== 'object') return false;
  const value = locator as Record<string, unknown>;
  if (value.kind === 'page_text' || value.kind === 'physical_line') {
    const location = value.kind === 'page_text' ? value.page_number : value.line_number;
    return Number.isInteger(location) && Number(location) > 0 && validOffsets(value);
  }
  if (value.kind === 'ooxml_text') {
    return validOffsets(value) && Array.isArray(value.block_path) && value.block_path.every(isValidBlockPathStep);
  }
  return value.kind === 'xlsx_cell' &&
    typeof value.sheet === 'string' && value.sheet.length > 0 &&
    typeof value.cell_range === 'string' && value.cell_range.length > 0 &&
    value.schema_version === 'locator:xlsx-cell:v1' &&
    Number.isInteger(value.row) && Number(value.row) > 0;
}

function validOffsets(value: Record<string, unknown>) {
  return Number.isInteger(value.start_codepoint) &&
    Number.isInteger(value.end_codepoint) &&
    Number(value.start_codepoint) >= 0 &&
    Number(value.end_codepoint) >= Number(value.start_codepoint);
}

function isValidBlockPathStep(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const step = value as Record<string, unknown>;
  return ['paragraph', 'table', 'row', 'cell'].includes(String(step.kind)) &&
    Number.isInteger(step.index) && Number(step.index) >= 0;
}

function formatLocator(locator: ParsedContentLocator): string {
  if (locator.kind === 'page_text') {
    return `${ru.documents.locatorPage} ${locator.page_number}, ${ru.documents.locatorOffsets} ${locator.start_codepoint}–${locator.end_codepoint}`;
  }
  if (locator.kind === 'physical_line') {
    return `${ru.documents.locatorLine} ${locator.line_number}, ${ru.documents.locatorOffsets} ${locator.start_codepoint}–${locator.end_codepoint}`;
  }
  if (locator.kind === 'xlsx_cell') {
    return `${ru.documents.locatorSheet} ${locator.sheet}, ${ru.documents.locatorRow} ${locator.row}, ${ru.documents.locatorCells} ${locator.cell_range}`;
  }
  return `${ru.documents.locatorPath} ${locator.block_path.map((step) => `${capitalize(step.kind)} ${step.index + 1}`).join(' / ')}, ${ru.documents.locatorOffsets} ${locator.start_codepoint}–${locator.end_codepoint}`;
}

function capitalize(value: string) {
  return value.slice(0, 1).toUpperCase() + value.slice(1);
}

function nextContentPageSearch(searchParams: URLSearchParams, cursor: string, revisionId: string) {
  const next = new URLSearchParams(searchParams);
  const revisionCursor = next.get('revision_cursor') ?? next.get('cursor');
  next.set('tab', 'content');
  next.set('revision', revisionId);
  next.delete('cursor');
  if (revisionCursor) next.set('revision_cursor', revisionCursor);
  next.set('content_cursor', cursor);
  return next.toString();
}

function DetailValue({
  code = false,
  label,
  value,
}: {
  code?: boolean;
  label: string;
  value: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={styles.value}>{code ? <code>{value}</code> : value}</dd>
    </div>
  );
}
