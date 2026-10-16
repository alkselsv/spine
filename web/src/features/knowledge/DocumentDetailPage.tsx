import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo } from 'react';
import { Link, useParams, useSearchParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient, SpineApiError } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { type AuthState, useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { DocumentAccessMetadata } from './DocumentAccessMetadata';
import {
  diagnosticAction,
  freshnessLabel,
  healthLabel,
  lifecycleLabel,
  processingStateLabel,
  publicationStatusLabel,
} from './documentLabels';
import { formatDate } from './format';
import { ContentInspectionTab } from './ParsedContentInspector';
import { ProjectionStatusPanel } from './ProjectionStatusPanel';
import { loadRevisionHistory } from './revision-api';
import { ProcessingCommandAction } from './ProcessingCommandAction';
import styles from './DocumentDetailPage.module.css';

type DocumentSnapshot = components['schemas']['DocumentSnapshot'];
type RevisionSnapshot = components['schemas']['RevisionSnapshot'];
type DocumentDetailTab = 'overview' | 'revisions' | 'content' | 'projection';

const DETAIL_TABS: readonly DocumentDetailTab[] = ['overview', 'revisions', 'content', 'projection'];

export function DocumentDetailPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const { documentId } = useParams();
  const [searchParams] = useSearchParams();
  const tab = parseDocumentDetailTab(searchParams.get('tab'));
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );
  const authorizationKey = `${getCredentialKey(auth.getBearerCredential)}:${auth.status}:${auth.role ?? ''}`;
  const documentQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadDocument(client, documentId ?? '', signal),
      queryKey: [...queryKey('document-detail'), documentId],
    }),
    enabled: Boolean(auth.getBearerCredential && documentId),
  });

  const scopeSearch = withScope(searchParams, scope);
  const backSearch = scopeSearch.toString();

  if (!auth.getBearerCredential) return <StatusPanel state="degraded" />;
  if (!documentId) return <StatusPanel state="failed" />;
  if (documentQuery.isPending) return <StatusPanel state="loading" />;
  if (documentQuery.error) {
    return (
      <section className={styles.page} aria-labelledby="document-detail-heading">
        <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
        <h2 id="document-detail-heading">{ru.documents.overviewTitle}</h2>
        <ApiErrorPanel error={documentQuery.error} />
        <div className={styles.actionRow}>
          <button
            className={styles.buttonLink}
            onClick={() => void documentQuery.refetch()}
            type="button"
          >
            {ru.documents.retry}
          </button>
          <Link
            className={styles.buttonLink}
            to={{ pathname: '/knowledge/documents', search: `?${backSearch}` }}
          >
            {ru.documents.backToList}
          </Link>
        </div>
      </section>
    );
  }
  if (!documentQuery.data) return <StatusPanel state="failed" />;

  return (
    <DocumentOverview
      client={client}
      document={documentQuery.data.document}
      documentId={documentId}
      authorizationKey={authorizationKey}
      searchParams={scopeSearch}
      scope={scope}
      queryKey={queryKey}
      traceId={documentQuery.data.traceId}
      tab={tab}
    />
  );
}
export async function loadDocument(
  client: ReturnType<typeof createScopedApiClient>,
  sourceObjectId: string,
  signal: AbortSignal,
): Promise<{ document: DocumentSnapshot; traceId: string }> {
  const result = await client.GET('/api/v1/documents/{source_object_id}', {
    params: { path: { source_object_id: sourceObjectId } },
    signal,
  });
  if (!result.data) throw new Error('The document detail response is unavailable.');
  return { document: result.data.data, traceId: result.data.trace_id };
}

export function parseDocumentDetailTab(value: string | null): DocumentDetailTab {
  return value && DETAIL_TABS.includes(value as DocumentDetailTab)
    ? (value as DocumentDetailTab)
    : 'overview';
}

function parseProjectionKind(value: string | null): string | null {
  return value && /^[a-z][a-z0-9_-]{0,63}$/.test(value) ? value : null;
}

export function documentOperationalState(document: DocumentSnapshot): string {
  const metadata = document.operational_metadata;
  const revision = document.current_revision;
  const publication = document.publication;
  const processing = metadata?.processing_state ?? latestProcessingState(revision);
  const lifecycle = metadata?.lifecycle ?? revision?.lifecycle;

  if (metadata?.status === 'unavailable' || publication?.status === 'unavailable') {
    return ru.documents.states.unavailable;
  }
  if (publication?.status === 'unpublished' || metadata?.publication_status === 'unpublished') {
    return ru.documents.states.unpublished;
  }
  if (lifecycle === 'tombstoned') return ru.documents.states.tombstoned;
  if (lifecycle === 'superseded') return ru.documents.states.superseded;
  if (processing === 'processing_failed') return ru.documents.states.processingFailed;
  if (publication?.health === 'degraded' || metadata?.health === 'degraded') {
    return ru.documents.filterOptions.publication_health.degraded;
  }
  if (publication?.freshness === 'stale' || metadata?.freshness === 'stale') {
    return ru.documents.states.stale;
  }
  if (lifecycle === 'processing' || processing === 'processing') {
    return ru.documents.states.parsing;
  }
  if (processing === 'projection_requested') return ru.documents.states.indexing;
  if (publication?.status === 'published' && (processing === 'processed' || lifecycle === 'current')) {
    return ru.documents.states.ready;
  }
  if (lifecycle === 'current') return ru.documents.filterOptions.lifecycle.current;
  return lifecycle ? lifecycleLabel(lifecycle) : ru.documents.unknown;
}

function DocumentOverview({
  client,
  document,
  documentId,
  authorizationKey,
  searchParams,
  scope,
  queryKey,
  traceId,
  tab,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  document: DocumentSnapshot;
  documentId: string;
  authorizationKey: string;
  searchParams: URLSearchParams;
  scope: { environmentId: string; workspaceId: string };
  queryKey: (resource: string) => readonly unknown[];
  traceId: string;
  tab: DocumentDetailTab;
}) {
  const revision = document.current_revision;
  const metadata = document.operational_metadata;
  const publication = document.publication;
  const detailSearch = new URLSearchParams(searchParams);
  detailSearch.delete('tab');
  detailSearch.delete('revision');
  detailSearch.delete('cursor');
  const runSearch = new URLSearchParams(searchParams);
  runSearch.delete('tab');
  runSearch.delete('cursor');
  runSearch.set('document', documentId);
  const projectionKind = parseProjectionKind(searchParams.get('projection_kind')) ?? publication?.projection_kind ?? 'qa';
  const overviewLink = detailLink(documentId, searchParams, 'overview');
  const historyLink = detailLink(documentId, searchParams, 'revisions');
  const contentLink = detailLink(documentId, searchParams, 'content');
  const projectionLink = detailLink(documentId, searchParams, 'projection', projectionKind);

  return (
    <section className={styles.page} aria-labelledby="document-detail-heading">
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
          <h2 id="document-detail-heading">{ru.documents.overviewTitle}</h2>
          <p className={styles.lede}>{ru.documents.overviewDescription}</p>
          <span className={styles.sourceId}>
            {ru.documents.detailSourceId}: <code>{document.source_object_id}</code>
          </span>
        </div>
        <span className={styles.statusPill}>{documentOperationalState(document)}</span>
      </header>

      <nav className={styles.navigation} aria-label={ru.documents.navigationLabel}>
        <Link
          aria-current={tab === 'overview' ? 'page' : undefined}
          className={tab === 'overview' ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink}
          to={overviewLink}
        >
          {ru.documents.overviewTab}
        </Link>
        <Link
          aria-current={tab === 'revisions' ? 'page' : undefined}
          className={tab === 'revisions' ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink}
          to={historyLink}
        >
          {ru.documents.revisionTimeline}
        </Link>
        <Link
          aria-current={tab === 'content' ? 'page' : undefined}
          className={tab === 'content' ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink}
          to={contentLink}
        >
          {ru.documents.contentInspectionTab}
        </Link>
        <Link
          aria-current={tab === 'projection' ? 'page' : undefined}
          className={tab === 'projection' ? `${styles.navLink} ${styles.navLinkActive}` : styles.navLink}
          to={projectionLink}
        >
          {ru.documents.projectionPanel}
        </Link>
      </nav>

      <div className={styles.grid}>
        <DocumentAccessMetadata metadata={metadata} scope={scope} traceId={traceId} />
      </div>

      {tab === 'revisions' ? (
        <RevisionTimeline
          client={client}
          document={document}
          documentId={documentId}
          queryKey={queryKey('document-processing-history')}
          searchParams={searchParams}
          scope={scope}
        />
      ) : null}

      {tab === 'content' ? (
        <ContentInspectionTab
          authorizationKey={authorizationKey}
          client={client}
          document={document}
          documentId={documentId}
          scope={scope}
        />
      ) : null}

      {tab === 'projection' ? (
        <ProjectionStatusPanel
          client={client}
          indexingState={metadata?.processing_state ?? latestProcessingState(revision)}
          projectionKind={projectionKind}
          queryKey={queryKey}
        />
      ) : tab === 'overview' ? <div className={styles.grid}>
        <section className={styles.card} aria-labelledby="source-object-heading">
          <h3 id="source-object-heading">{ru.documents.sourceObject}</h3>
          <dl className={styles.details}>
            <DetailValue label={ru.documents.sourceKind} value={sourceKindLabel(document.source_kind)} />
            <DetailValue label={ru.documents.createdAt} value={formatDate(document.created_at)} />
            {document.identity?.upload_identity ? (
              <DetailValue label={ru.documents.sourceIdentity} value={document.identity.upload_identity} />
            ) : null}
            <DetailValue label={ru.documents.publication} value={publicationStatusLabel(publication?.status)} />
          </dl>
        </section>

        <section className={styles.card} aria-labelledby="operational-state-heading">
          <div className={styles.statusHeader}>
            <h3 id="operational-state-heading">{ru.documents.processing}</h3>
            <span className={styles.statusPill}>{operationalStatusLabel(metadata?.status)}</span>
          </div>
          <dl className={styles.details}>
            <DetailValue label={ru.documents.lifecycle} value={lifecycleLabel(metadata?.lifecycle ?? revision?.lifecycle)} />
            <DetailValue label={ru.documents.processingState} value={processingStateLabel(metadata?.processing_state ?? latestProcessingState(revision))} />
            <DetailValue label={ru.documents.freshness} value={freshnessLabel(publication?.freshness ?? metadata?.freshness)} />
            <DetailValue label={ru.documents.projectionHealth} value={healthLabel(publication?.health ?? metadata?.health)} />
          </dl>
        </section>

        <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="revision-heading">
          <h3 id="revision-heading">{ru.documents.currentRevision}</h3>
          <p className={styles.immutable}>{ru.documents.immutableRevision}</p>
          {revision ? <RevisionDetails revision={revision} /> : <StatusPanel state="degraded" />}
        </section>

        <section className={styles.card} aria-labelledby="processing-heading">
          <h3 id="processing-heading">{ru.documents.processing}</h3>
          <p className={styles.cardDescription}>{ru.documents.processingState}: {processingStateLabel(metadata?.processing_state ?? latestProcessingState(revision))}</p>
          {revision?.processing_references.length ? (
            <ul className={styles.referenceList}>
              {revision.processing_references.map((reference) => (
                <li className={styles.reference} key={`${reference.ingestion_run_id}-${reference.ingestion_item_id}`}>
                  <strong>{processingStateLabel(reference.state)}</strong>
                  <span className={styles.referenceMeta}>
                    {ru.documents.coverage}: {coverageLabel(reference.coverage)}
                  </span>
                  {reference.processing_receipt_id ? (
                    <span className={styles.referenceMeta}>
                      {ru.documents.processingReceipt}: <code>{reference.processing_receipt_id}</code>
                    </span>
                  ) : null}
                  <Link
                    className={styles.link}
                    to={{ pathname: `/runs/${reference.ingestion_run_id}`, search: `?${runSearch.toString()}` }}
                  >
                    {ru.documents.processingRun}
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.cardDescription}>{ru.documents.noProcessingRuns}</p>
          )}
        </section>

        <section className={styles.card} aria-labelledby="projection-heading">
          <h3 id="projection-heading">{ru.documents.projection}</h3>
          <p className={styles.cardDescription}>{ru.documents.projectionKind}: {publication?.projection_kind ?? ru.documents.unknown}</p>
          <dl className={styles.details}>
            <DetailValue label={ru.documents.publication} value={publicationStatusLabel(publication?.status)} />
            <DetailValue label={ru.documents.freshness} value={freshnessLabel(publication?.freshness)} />
            <DetailValue label={ru.documents.projectionHealth} value={healthLabel(publication?.health)} />
            <DetailValue label={ru.documents.drift} value={publication?.drift_count === undefined || publication.drift_count === null ? ru.documents.unknown : String(publication.drift_count)} />
            <DetailValue label={ru.documents.snapshot} value={publication?.snapshot_id ?? ru.documents.unknown} code={Boolean(publication?.snapshot_id)} />
            <DetailValue label={ru.documents.manifest} value={booleanLabel(publication?.manifest_complete)} />
            <DetailValue label={ru.documents.sourceCovered} value={booleanLabel(publication?.source_covered)} />
          </dl>
        </section>

        {metadata?.diagnostic_code ? (
          <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="diagnostic-heading">
            <h3 id="diagnostic-heading">{ru.documents.diagnostics}</h3>
            <dl className={styles.details}>
              <DetailValue label={ru.documents.nextAction} value={diagnosticAction(metadata.diagnostic_code)} />
            </dl>
          </section>
        ) : null}
      </div> : null}

      <div className={styles.actionRow}>
        <Link className={styles.buttonLink} to={{ pathname: '/knowledge/documents', search: `?${detailSearch.toString()}` }}>
          {ru.documents.backToList}
        </Link>
      </div>
    </section>
  );
}

function RevisionTimeline({
  client,
  document,
  documentId,
  queryKey,
  searchParams,
  scope,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  document: DocumentSnapshot;
  documentId: string;
  queryKey: readonly unknown[];
  searchParams: URLSearchParams;
  scope: { environmentId: string; workspaceId: string };
}) {
  const [currentSearch, setSearchParams] = useSearchParams();
  const cursor = currentSearch.get('cursor') ?? undefined;
  const selectedRevisionId = currentSearch.get('revision') ?? document.current_revision?.source_revision_id ?? null;
  const revisionsQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadRevisionHistory(client, documentId, cursor, signal),
      queryKey: [
        'spine',
        'document-revisions',
        scope.workspaceId,
        scope.environmentId,
        documentId,
        cursor,
      ],
    }),
    enabled: true,
  });

  useEffect(() => {
    if (!currentSearch.has('revision') && document.current_revision?.source_revision_id) {
      const next = new URLSearchParams(currentSearch);
      next.set('tab', 'revisions');
      next.set('revision', document.current_revision.source_revision_id);
      setSearchParams(next, { replace: true });
    }
  }, [currentSearch, document.current_revision?.source_revision_id, setSearchParams]);

  const resetCursor = () => {
    const next = new URLSearchParams(currentSearch);
    next.delete('cursor');
    setSearchParams(next);
  };

  if (revisionsQuery.isPending) return <StatusPanel state="loading" />;
  if (revisionsQuery.error) {
    return (
      <section className={styles.card} aria-labelledby="revision-history-heading">
        <h3 id="revision-history-heading">{ru.documents.revisionTimeline}</h3>
        <ApiErrorPanel error={revisionsQuery.error} />
        {cursor && revisionsQuery.error instanceof SpineApiError && revisionsQuery.error.kind === 'validation' ? (
          <button className={styles.buttonLink} onClick={resetCursor} type="button">
            {ru.documents.resetRevisionCursor}
          </button>
        ) : (
          <button className={styles.buttonLink} onClick={() => void revisionsQuery.refetch()} type="button">
            {ru.documents.retry}
          </button>
        )}
      </section>
    );
  }
  if (!revisionsQuery.data) return <StatusPanel state="failed" />;

  const revisions = [...revisionsQuery.data.items].sort((left, right) =>
    right.observed_at.localeCompare(left.observed_at) ||
    right.source_revision_id.localeCompare(left.source_revision_id),
  );
  const selectedRevision = revisions.find((revision) => revision.source_revision_id === selectedRevisionId);
  const safeSearch = withScope(searchParams, scope);
  safeSearch.set('tab', 'revisions');

  return (
    <section className={`${styles.card} ${styles.cardWide}`} aria-labelledby="revision-history-heading">
      <header className={styles.statusHeader}>
        <div>
          <h3 id="revision-history-heading">{ru.documents.revisionTimeline}</h3>
          <p className={styles.cardDescription}>{ru.documents.immutableRevision}</p>
        </div>
        <span className={styles.statusPill}>{ru.documents.selectedRevision}: {selectedRevisionId ?? ru.documents.unknown}</span>
      </header>
      <p className={styles.protectedNotice} role="note">{ru.documents.protectedRevisionDetails}</p>
      {selectedRevision ? (
        <SelectedRevision
          client={client}
          document={document}
          documentId={documentId}
          queryKey={queryKey}
          revision={selectedRevision}
          searchParams={safeSearch}
        />
      ) : selectedRevisionId ? (
        <p className={styles.subviewNotice} role="status">{ru.documents.selectedRevisionUnavailable}</p>
      ) : null}
      {revisions.length === 0 ? (
        <StatusPanel state="empty" />
      ) : (
        <ol className={styles.revisionList} aria-label={ru.documents.revisionTimeline}>
          {revisions.map((revision) => {
            const rowSearch = new URLSearchParams(safeSearch);
            rowSearch.set('revision', revision.source_revision_id);
            return (
              <li className={styles.revisionRow} key={revision.source_revision_id}>
                <div className={styles.revisionHeading}>
                  <Link
                    aria-current={revision.source_revision_id === selectedRevisionId ? 'true' : undefined}
                    className={styles.revisionId}
                    to={{ pathname: `/knowledge/documents/${documentId}`, search: `?${rowSearch.toString()}` }}
                  >
                    <code>{revision.source_revision_id}</code>
                  </Link>
                  <span className={`${styles.statusPill} ${revisionStateClass(revision.lifecycle)}`}>
                    {lifecycleLabel(revision.lifecycle)}
                  </span>
                  {revision.kind === 'tombstone' ? <span className={styles.tombstoneMark}>{ru.documents.tombstoneRevision}</span> : null}
                </div>
                <dl className={styles.details}>
                  <DetailValue label={ru.documents.observedAt} value={formatDate(revision.observed_at)} />
                  <DetailValue label={ru.documents.acceptedAt} value={formatDate(revision.accepted_at)} />
                  <DetailValue label={ru.documents.admissionStatus} value={admissionStatusLabel(revision.admission_status)} />
                  <DetailValue label={ru.documents.predecessor} value={revision.predecessor_revision_id ?? ru.documents.unknown} code={Boolean(revision.predecessor_revision_id)} />
                  <DetailValue label={ru.documents.supersededBy} value={revision.superseded_by_revision_id ?? ru.documents.unknown} code={Boolean(revision.superseded_by_revision_id)} />
                  {revision.deletion_reason ? <DetailValue label={ru.documents.deletionReason} value={safeDeletionReason(revision.deletion_reason)} /> : null}
                </dl>
                <RevisionReferences revision={revision} />
              </li>
            );
          })}
        </ol>
      )}
      {revisionsQuery.data.next_cursor ? (
        <Link
          className={styles.buttonLink}
          to={{
            pathname: `/knowledge/documents/${documentId}`,
            search: `?${nextRevisionPageSearch(safeSearch, revisionsQuery.data.next_cursor, selectedRevisionId)}`,
          }}
        >
          {ru.documents.nextPage}
        </Link>
      ) : null}
    </section>
  );
}

function SelectedRevision({
  client,
  document,
  documentId,
  queryKey,
  revision,
  searchParams,
}: {
  client: ReturnType<typeof createScopedApiClient>;
  document: DocumentSnapshot;
  documentId: string;
  queryKey: readonly unknown[];
  revision: RevisionSnapshot;
  searchParams: URLSearchParams;
}) {
  const contentSearch = new URLSearchParams(searchParams);
  contentSearch.set('tab', 'content');
  contentSearch.set('revision', revision.source_revision_id);
  const runSearch = new URLSearchParams(searchParams);
  runSearch.delete('tab');
  runSearch.delete('cursor');
  runSearch.set('document', documentId);
  return (
    <section className={styles.selectedRevision} aria-labelledby="selected-revision-heading">
      <h4 id="selected-revision-heading">{ru.documents.selectedRevision}</h4>
      <RevisionDetails revision={revision} />
      <div className={styles.actionRow}>
        <Link
          className={styles.buttonLink}
          to={{ pathname: `/knowledge/documents/${documentId}`, search: `?${contentSearch.toString()}` }}
        >
          {ru.documents.inspectContent}
        </Link>
        {revision.processing_references.map((reference) => (
          <Link
            className={styles.buttonLink}
            key={`${reference.ingestion_run_id}-${reference.ingestion_item_id}`}
            to={{ pathname: `/runs/${reference.ingestion_run_id}`, search: `?${runSearch.toString()}` }}
          >
            {ru.documents.processingRun}
          </Link>
        ))}
      </div>
      {revision.kind === 'content' &&
      revision.lifecycle === 'current' &&
      document.current_revision?.source_revision_id === revision.source_revision_id ? (
        <ProcessingCommandAction
          client={client}
          currentRevisionId={revision.source_revision_id}
          mode={{ kind: 'reprocess' }}
          processingReceiptId={revision.processing_references.at(-1)?.processing_receipt_id}
          queryKey={queryKey}
          sourceObjectId={document.source_object_id}
          sourceRevisionId={revision.source_revision_id}
        />
      ) : null}
    </section>
  );
}

function RevisionReferences({ revision }: { revision: RevisionSnapshot }) {
  if (revision.processing_references.length === 0 && revision.projection_references.length === 0) return null;
  return (
    <div className={styles.revisionReferences}>
      {revision.processing_references.map((reference) => (
        <span key={`${reference.ingestion_run_id}-${reference.ingestion_item_id}`}>
          {ru.documents.processing}: {processingStateLabel(reference.state)} · {ru.documents.processingRunReference}: {reference.ingestion_run_id} · {ru.documents.processingReceipt}: {reference.processing_receipt_id ?? ru.documents.unknown}
        </span>
      ))}
      {revision.projection_references.map((reference) => (
        <span key={`${reference.projection_run_id}-${reference.projection_candidate_id}`}>
          {ru.documents.projection}: {reference.projection_kind} · {reference.state} · {ru.documents.projectionRunReference}: {reference.projection_run_id} · {reference.projection_reference_id ?? reference.projection_candidate_id}
        </span>
      ))}
    </div>
  );
}

function nextRevisionPageSearch(searchParams: URLSearchParams, cursor: string, selectedRevisionId: string | null) {
  const next = new URLSearchParams(searchParams);
  next.set('cursor', cursor);
  if (selectedRevisionId) next.set('revision', selectedRevisionId);
  return next.toString();
}

function revisionStateClass(lifecycle: components['schemas']['RevisionLifecycle']) {
  if (lifecycle === 'current') return styles.revisionStateCurrent;
  if (lifecycle === 'superseded') return styles.revisionStateSuperseded;
  if (lifecycle === 'tombstoned') return styles.revisionStateTombstoned;
  if (lifecycle === 'rejected' || lifecycle === 'quarantined') return styles.revisionStateRejected;
  return styles.revisionStateProcessing;
}

function RevisionDetails({ revision }: { revision: RevisionSnapshot }) {
  return (
    <dl className={styles.details}>
      <DetailValue label={ru.documents.revisionId} value={revision.source_revision_id} code />
      <DetailValue label={ru.documents.lifecycle} value={lifecycleLabel(revision.lifecycle)} />
      <DetailValue label={ru.documents.admissionStatus} value={admissionStatusLabel(revision.admission_status)} />
      <DetailValue label={ru.documents.observedAt} value={formatDate(revision.observed_at)} />
      <DetailValue label={ru.documents.acceptedAt} value={formatDate(revision.accepted_at)} />
      <DetailValue label={ru.documents.acceptedGeneration} value={revision.accepted_generation ? String(revision.accepted_generation) : ru.documents.unknown} />
      <DetailValue label={ru.documents.predecessor} value={revision.predecessor_revision_id ?? ru.documents.unknown} code={Boolean(revision.predecessor_revision_id)} />
      <DetailValue label={ru.documents.supersededBy} value={revision.superseded_by_revision_id ?? ru.documents.unknown} code={Boolean(revision.superseded_by_revision_id)} />
      {revision.deletion_reason ? <DetailValue label={ru.documents.deletionReason} value={safeDeletionReason(revision.deletion_reason)} /> : null}
    </dl>
  );
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

function detailLink(documentId: string, searchParams: URLSearchParams, tab: DocumentDetailTab, projectionKind?: string) {
  const search = new URLSearchParams(searchParams);
  search.set('tab', tab);
  if (projectionKind) search.set('projection_kind', projectionKind);
  return { pathname: `/knowledge/documents/${documentId}`, search: `?${search.toString()}` };
}

function withScope(searchParams: URLSearchParams, scope: { environmentId: string; workspaceId: string }) {
  const next = new URLSearchParams(searchParams);
  next.set('workspace', scope.workspaceId);
  next.set('environment', scope.environmentId);
  return next;
}

const credentialKeys = new WeakMap<NonNullable<AuthState['getBearerCredential']>, number>();
let nextCredentialKey = 1;

function getCredentialKey(credential: AuthState['getBearerCredential']) {
  if (!credential) return 0;
  const existingKey = credentialKeys.get(credential);
  if (existingKey) return existingKey;
  const newKey = nextCredentialKey++;
  credentialKeys.set(credential, newKey);
  return newKey;
}

function latestProcessingState(revision: RevisionSnapshot | null | undefined) {
  return revision?.processing_references.at(-1)?.state;
}

function operationalStatusLabel(value: components['schemas']['OperationalMetadataStatus'] | null | undefined) {
  if (value === 'available') return ru.documents.available;
  if (value === 'degraded') return ru.documents.filterOptions.publication_health.degraded;
  if (value === 'unavailable') return ru.documents.states.unavailable;
  return ru.documents.unknown;
}

function sourceKindLabel(value: string) {
  if (value === 'document') return ru.documents.filterOptions.source_kind.document;
  if (value === 'message') return ru.documents.filterOptions.source_kind.message;
  if (value === 'record') return ru.documents.filterOptions.source_kind.record;
  return ru.documents.sourceFallback;
}

function admissionStatusLabel(value: components['schemas']['AdmissionDisposition'] | null | undefined) {
  if (value === 'admitted') return 'Допущена';
  if (value === 'quarantined') return ru.documents.filterOptions.lifecycle.quarantined;
  if (value === 'rejected') return ru.documents.filterOptions.lifecycle.rejected;
  return ru.documents.unknown;
}

function coverageLabel(value: components['schemas']['ProcessingCoverage'] | null | undefined) {
  if (value === 'complete') return 'Полное';
  if (value === 'partial') return 'Частичное';
  if (value === 'failed') return ru.documents.states.processingFailed;
  return ru.documents.unknown;
}

function booleanLabel(value: boolean | null | undefined) {
  if (value === true) return ru.documents.yes;
  if (value === false) return ru.documents.no;
  return ru.documents.unknown;
}

function safeDeletionReason(value: string) {
  return value === 'source_deleted' ? 'Источник удалён' : ru.documents.unknown;
}
