import { Select } from '@base-ui/react';
import { useQuery } from '@tanstack/react-query';
import type { components, operations } from '../../../api/generated/schema';
import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router';

import { createScopedApiClient, type SpineApiError } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel, type InterfaceState } from '../../app/status';
import { useScope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { DocumentUpload } from './DocumentUpload';
import styles from './DocumentsPage.module.css';

type DocumentSnapshot = components['schemas']['DocumentSnapshot'];
type DocumentCollection = components['schemas']['ApiCollectionEnvelope_DocumentSnapshot_'];
type SourceKind = components['schemas']['SourceKind'];
type RevisionLifecycle = components['schemas']['RevisionLifecycle'];
type IngestionItemState = components['schemas']['IngestionItemState'];
type PublicationStatus = components['schemas']['DocumentPublicationStatus'];
type ProjectionHealth = components['schemas']['ProjectionHealth'];
type DocumentsQuery = NonNullable<operations['documents.list']['parameters']['query']>;

export type DocumentFilters = {
  source_kind?: SourceKind;
  lifecycle?: RevisionLifecycle;
  processing_state?: IngestionItemState;
  publication_status?: PublicationStatus;
  publication_health?: ProjectionHealth;
};

export type DocumentQueryState = DocumentFilters & { cursor?: string };

const PAGE_LIMIT = 25;
const FILTER_KEYS = [
  'source_kind',
  'lifecycle',
  'processing_state',
  'publication_status',
  'publication_health',
] as const satisfies readonly (keyof DocumentFilters)[];

const FILTER_OPTIONS: {
  [Key in keyof DocumentFilters]: readonly { value: NonNullable<DocumentFilters[Key]>; label: string }[];
} = {
  source_kind: [
    { value: 'document', label: ru.documents.filterOptions.source_kind.document },
    { value: 'message', label: ru.documents.filterOptions.source_kind.message },
    { value: 'record', label: ru.documents.filterOptions.source_kind.record },
  ],
  lifecycle: [
    { value: 'observed', label: ru.documents.filterOptions.lifecycle.observed },
    { value: 'quarantined', label: ru.documents.filterOptions.lifecycle.quarantined },
    { value: 'rejected', label: ru.documents.filterOptions.lifecycle.rejected },
    { value: 'superseded', label: ru.documents.filterOptions.lifecycle.superseded },
    { value: 'current', label: ru.documents.filterOptions.lifecycle.current },
    { value: 'tombstoned', label: ru.documents.filterOptions.lifecycle.tombstoned },
    { value: 'processing', label: ru.documents.filterOptions.lifecycle.processing },
  ],
  processing_state: [
    { value: 'received', label: ru.documents.filterOptions.processing_state.received },
    { value: 'stored', label: ru.documents.filterOptions.processing_state.stored },
    { value: 'observed', label: ru.documents.filterOptions.processing_state.observed },
    { value: 'admission_pending', label: ru.documents.filterOptions.processing_state.admission_pending },
    { value: 'quarantined', label: ru.documents.filterOptions.processing_state.quarantined },
    { value: 'rejected', label: ru.documents.filterOptions.processing_state.rejected },
    { value: 'accepted', label: ru.documents.filterOptions.processing_state.accepted },
    { value: 'processing', label: ru.documents.filterOptions.processing_state.processing },
    { value: 'processed', label: ru.documents.filterOptions.processing_state.processed },
    { value: 'processing_failed', label: ru.documents.filterOptions.processing_state.processing_failed },
    { value: 'projection_requested', label: ru.documents.filterOptions.processing_state.projection_requested },
  ],
  publication_status: [
    { value: 'published', label: ru.documents.filterOptions.publication_status.published },
    { value: 'unpublished', label: ru.documents.filterOptions.publication_status.unpublished },
    { value: 'unavailable', label: ru.documents.filterOptions.publication_status.unavailable },
  ],
  publication_health: [
    { value: 'healthy', label: ru.documents.filterOptions.publication_health.healthy },
    { value: 'degraded', label: ru.documents.filterOptions.publication_health.degraded },
  ],
};

const FILTER_LABELS: { [Key in keyof DocumentFilters]: string } = {
  source_kind: ru.documents.filters.source_kind,
  lifecycle: ru.documents.filters.lifecycle,
  processing_state: ru.documents.filters.processing_state,
  publication_status: ru.documents.filters.publication_status,
  publication_health: ru.documents.filters.publication_health,
};

export function parseDocumentQuery(search: string): DocumentQueryState {
  const parameters = new URLSearchParams(search);
  const state: DocumentQueryState = {};

  for (const key of FILTER_KEYS) {
    const value = parameters.get(key);
    if (value && isFilterValue(key, value)) Object.assign(state, { [key]: value });
  }

  const cursor = parameters.get('cursor');
  if (cursor && cursor.length <= 768) state.cursor = cursor;
  return state;
}

export function updateDocumentQuery(search: string, next: DocumentQueryState): string {
  const parameters = new URLSearchParams(search);
  for (const key of [...FILTER_KEYS, 'cursor']) parameters.delete(key);
  for (const key of FILTER_KEYS) {
    const value = next[key];
    if (value) parameters.set(key, value);
  }
  if (next.cursor) parameters.set('cursor', next.cursor);
  return parameters.toString();
}

export function DocumentsPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryState = parseDocumentQuery(`?${searchParams.toString()}`);
  const client = useMemo(
    () =>
      createScopedApiClient({
        baseUrl: window.location.origin,
        getBearerCredential: auth.getBearerCredential ?? (() => ''),
        scope,
      }),
    [auth.getBearerCredential, scope],
  );
  const documentsQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadDocuments(client, queryState, signal),
      queryKey: [...queryKey('documents'), queryState],
    }),
    enabled: Boolean(auth.getBearerCredential),
  });

  const hasFilters = FILTER_KEYS.some((key) => queryState[key] !== undefined);
  const scopeSearchParameters = new URLSearchParams({
    workspace: scope.workspaceId,
    environment: scope.environmentId,
  });
  const fixture = searchParams.get('fixture');
  if (fixture) scopeSearchParameters.set('fixture', fixture);
  const scopeSearch = scopeSearchParameters.toString();
  const state = documentsState(documentsQuery.data, documentsQuery.error, documentsQuery.isStale);

  function changeFilter<Key extends keyof DocumentFilters>(key: Key, value: string) {
    const next = { ...queryState } as DocumentQueryState;
    delete next.cursor;
    if (value && isFilterValue(key, value)) next[key] = value;
    else delete next[key];
    setSearchParams(updateDocumentQuery(searchParams.toString(), next), { replace: true });
  }

  function clearFilters() {
    setSearchParams(updateDocumentQuery(searchParams.toString(), {}), { replace: true });
  }

  function nextPage() {
    if (!documentsQuery.data?.next_cursor) return;
    setSearchParams(
      updateDocumentQuery(searchParams.toString(), {
        ...queryState,
        cursor: documentsQuery.data.next_cursor,
      }),
    );
  }

  return (
    <section className={styles.page} aria-labelledby="documents-heading">
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
          <h2 id="documents-heading">{ru.documents.title}</h2>
          <p className={styles.lede}>{ru.documents.description}</p>
        </div>
        {hasFilters ? (
          <button className={styles.secondaryButton} onClick={clearFilters} type="button">
            {ru.documents.clearFilters}
          </button>
        ) : null}
      </div>

      <DocumentUpload
        key={`${scope.workspaceId}:${scope.environmentId}`}
        client={client}
        disabled={!auth.getBearerCredential}
        scopeSearch={scopeSearch}
      />

      <div className={styles.filters} aria-label={ru.documents.filtersLabel}>
        {FILTER_KEYS.map((key) => (
          <FilterSelect
            key={key}
            label={FILTER_LABELS[key]!}
            options={FILTER_OPTIONS[key]!}
            value={queryState[key]}
            onValueChange={(value) => changeFilter(key, value)}
          />
        ))}
      </div>

      {!auth.getBearerCredential ? (
        <>
          <StatusPanel state="degraded" />
          <DocumentActions scopeSearch={scopeSearch} />
        </>
      ) : documentsQuery.error ? (
        <div>
          <ApiErrorPanel error={documentsQuery.error} />
          <DocumentActions onRetry={() => void documentsQuery.refetch()} scopeSearch={scopeSearch} />
        </div>
      ) : documentsQuery.isPending ? (
        <>
          <StatusPanel state="loading" />
          <DocumentActions onRetry={() => void documentsQuery.refetch()} scopeSearch={scopeSearch} />
        </>
      ) : documentsQuery.data ? (
        <DocumentResults
          data={documentsQuery.data}
          onNextPage={nextPage}
          scopeSearch={scopeSearch}
          state={state}
          onRetry={() => void documentsQuery.refetch()}
          search={searchParams.toString()}
        />
      ) : (
        <StatusPanel state="failed" />
      )}
    </section>
  );
}

const ALL_FILTER_VALUE = '__all__';

function FilterSelect({
  label,
  onValueChange,
  options,
  value,
}: {
  label: string;
  onValueChange: (value: string) => void;
  options: readonly { value: string; label: string }[];
  value: string | undefined;
}) {
  const items = [{ value: ALL_FILTER_VALUE, label: ru.documents.all }, ...options];
  return (
    <label className={styles.filter}>
      <span>{label}</span>
      <Select.Root
        items={items}
        value={value ?? ALL_FILTER_VALUE}
        onValueChange={(nextValue) => onValueChange(nextValue === ALL_FILTER_VALUE ? '' : (nextValue ?? ''))}
      >
        <Select.Trigger className={styles.filterTrigger} aria-label={label}>
          <Select.Value />
          <Select.Icon className={styles.filterIcon}>⌄</Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner className={styles.filterPositioner} sideOffset={4}>
            <Select.Popup className={styles.filterPopup}>
              <Select.List>
                {items.map((option) => (
                  <Select.Item className={styles.filterOption} key={option.value} value={option.value}>
                    <Select.ItemText>{option.label}</Select.ItemText>
                  </Select.Item>
                ))}
              </Select.List>
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
    </label>
  );
}

function DocumentActions({
  onRetry,
  scopeSearch,
}: {
  onRetry?: () => void;
  scopeSearch: string;
}) {
  return (
    <div className={styles.actions}>
      {onRetry ? (
        <button className={styles.secondaryButton} onClick={onRetry} type="button">
          {ru.documents.retry}
        </button>
      ) : null}
      <Link className={styles.secondaryButton} to={{ pathname: '/', search: scopeSearch }}>
        {ru.documents.backToOverview}
      </Link>
    </div>
  );
}

function DocumentResults({
  data,
  onNextPage,
  onRetry,
  search,
  scopeSearch,
  state,
}: {
  data: DocumentCollection;
  onNextPage: () => void;
  onRetry: () => void;
  search: string;
  scopeSearch: string;
  state: InterfaceState;
}) {
  if (data.items.length === 0) {
    return (
      <div>
        <StatusPanel state="empty" />
        <DocumentActions onRetry={onRetry} scopeSearch={scopeSearch} />
      </div>
    );
  }

  return (
    <>
      {state !== 'ready' ? (
        <>
          <StatusPanel state={state} />
          <DocumentActions onRetry={onRetry} scopeSearch={scopeSearch} />
        </>
      ) : null}
      <div className={styles.tableFrame}>
        <table className={styles.table}>
          <caption className={styles.visuallyHidden}>{ru.documents.tableCaption}</caption>
          <thead>
            <tr>
              <th scope="col">{ru.documents.columns.type}</th>
              <th scope="col">{ru.documents.columns.state}</th>
              <th scope="col">{ru.documents.columns.publication}</th>
              <th scope="col">{ru.documents.columns.health}</th>
              <th scope="col">{ru.documents.columns.updated}</th>
              <th scope="col"><span className={styles.visuallyHidden}>{ru.documents.columns.action}</span></th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((document) => (
              <DocumentRow document={document} key={document.source_object_id} search={search} />
            ))}
          </tbody>
        </table>
      </div>
      <div className={styles.pagination}>
        <span>{ru.documents.pageLimit.replace('{count}', String(data.items.length))}</span>
        {data.next_cursor ? (
          <button className={styles.secondaryButton} onClick={onNextPage} type="button">
            {ru.documents.nextPage}
          </button>
        ) : null}
      </div>
    </>
  );
}

function DocumentRow({ document, search }: { document: DocumentSnapshot; search: string }) {
  const metadata = document.operational_metadata;
  const publication = document.publication;
  const lifecycle = metadata?.lifecycle ?? document.current_revision?.lifecycle;
  const processing = metadata?.processing_state;
  const state = documentStateLabel(lifecycle, processing, publication?.status);
  const detailSearch = search ? `?${search}` : '';

  return (
    <tr>
      <td>
        <span className={styles.primaryCell}>{sourceKindLabel(document.source_kind)}</span>
        <span className={styles.secondaryCell}>
          {ru.documents.sourceId}: {document.source_object_id}
        </span>
      </td>
      <td><span className={styles.statusPill}>{state}</span></td>
      <td>{publicationStatusLabel(publication?.status)}</td>
      <td>{healthLabel(publication?.health ?? metadata?.health, publication?.freshness ?? metadata?.freshness)}</td>
      <td>{formatDate(document.current_revision?.observed_at ?? document.created_at)}</td>
      <td>
        <Link
          className={styles.rowLink}
          to={{ pathname: `/knowledge/documents/${document.source_object_id}`, search: detailSearch }}
        >
          {ru.documents.open}
        </Link>
      </td>
    </tr>
  );
}

async function loadDocuments(
  client: ReturnType<typeof createScopedApiClient>,
  state: DocumentQueryState,
  signal: AbortSignal,
): Promise<DocumentCollection> {
  const query: DocumentsQuery = { limit: PAGE_LIMIT };
  if (state.cursor) query.cursor = state.cursor;
  for (const key of FILTER_KEYS) {
    const value = state[key];
    if (value) Object.assign(query, { [key]: value });
  }
  const result = await client.GET('/api/v1/documents', { params: { query }, signal });
  if (!result.data) throw new Error('The document collection response is unavailable.');
  return result.data;
}

export function documentsState(
  data: DocumentCollection | undefined,
  error: SpineApiError | null,
  isStale: boolean,
): InterfaceState {
  if (error) {
    if (error.kind === 'authorization') return 'permissionDenied';
    if (error.kind === 'validation') return 'validationFailed';
    if (error.kind === 'unavailable') return 'degraded';
    return 'failed';
  }
  if (!data || data.items.length === 0) return 'empty';
  if (isStale) return 'stale';
  if (data.items.some((item) => item.operational_metadata?.status !== 'available')) return 'partial';
  return 'ready';
}

function isFilterValue<Key extends keyof DocumentFilters>(key: Key, value: string): value is NonNullable<DocumentFilters[Key]> {
  return FILTER_OPTIONS[key]!.some((option) => option.value === value);
}

function sourceKindLabel(value: string): string {
  return filterOptionLabel('source_kind', value) ?? ru.documents.sourceFallback;
}

function publicationStatusLabel(value: PublicationStatus | null | undefined): string {
  if (!value) return ru.documents.unknown;
  return filterOptionLabel('publication_status', value) ?? ru.documents.unknown;
}

function healthLabel(
  health: ProjectionHealth | null | undefined,
  freshness: components['schemas']['ProjectionFreshness'] | null | undefined,
): string {
  if (health === 'degraded') return ru.documents.filterOptions.publication_health.degraded;
  if (health === 'healthy' && freshness === 'stale') return ru.documents.states.stale;
  if (health === 'healthy') return ru.documents.filterOptions.publication_health.healthy;
  return ru.documents.unknown;
}

function documentStateLabel(
  lifecycle: RevisionLifecycle | null | undefined,
  processing: IngestionItemState | null | undefined,
  publication: PublicationStatus | null | undefined,
): string {
  if (publication === 'unavailable') return ru.documents.states.unavailable;
  if (publication === 'unpublished') return ru.documents.states.unpublished;
  if (lifecycle === 'rejected' || processing === 'rejected') return ru.documents.states.rejected;
  if (lifecycle === 'tombstoned') return ru.documents.states.tombstoned;
  if (processing === 'processing_failed') return ru.documents.states.processingFailed;
  if (String(processing) === 'cancelled') return ru.documents.states.cancelled;
  if (processing === 'projection_requested') return ru.documents.states.indexing;
  if (lifecycle === 'processing' || processing === 'processing') return ru.documents.states.parsing;
  if (publication === 'published' && (processing === 'processed' || lifecycle === 'current')) return ru.documents.states.ready;
  if (processing) return ru.documents.states.uploaded;
  if (lifecycle) return lifecycleLabel(lifecycle);
  return ru.documents.unknown;
}

function lifecycleLabel(value: RevisionLifecycle): string {
  return filterOptionLabel('lifecycle', value) ?? ru.documents.unknown;
}

function filterOptionLabel(key: keyof DocumentFilters, value: string): string | undefined {
  return FILTER_OPTIONS[key]!.find((option) => option.value === value)?.label;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return ru.documents.unknown;
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium' }).format(date);
}
