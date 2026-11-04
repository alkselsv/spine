import { useInfiniteQuery, useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import { Check, ChevronLeft, ChevronRight, CircleAlert, LockKeyhole, Mail, Plus, RefreshCw, ShieldCheck, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from 'react';
import { useSearchParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient, SpineApiError } from '../../api/client';
import { useAuth } from '../../app/auth';
import { useScope } from '../../app/scope';
import { usePal } from '../../ported/halaska-kit';
import styles from './ProviderConnectionsPage.module.css';

type Connection = components['schemas']['ProviderConnectionResponse'];
type Health = Connection['health'];
type MailboxSync = NonNullable<Connection['mailbox_sync']>;
type Operation = components['schemas']['EmailProviderOperation'];
type Capability = components['schemas']['ProviderCapabilityKind'];
type ProviderKind = Connection['provider_kind'];
interface ProviderPresentation { description: string; family: string; label: string }
interface ConfigurationDraft { approvalId: string; connectionId: string; expectedVersion: number; predecessorVersionId: string | null }
const emptyConfigurationDraft: ConfigurationDraft = { approvalId: '', connectionId: '', expectedVersion: 0, predecessorVersionId: null };

const healthCopy: Record<Health, string> = {
  healthy: 'Работает штатно', degraded: 'Работает с ограничениями', revoked: 'Доступ отозван', reconciliation_required: 'Требуется сверка',
};
const syncCopy: Record<MailboxSync['state'], string> = {
  not_started: 'Синхронизация не запускалась', pending: 'Синхронизация ожидает запуска', running: 'Синхронизация выполняется', complete: 'Синхронизация завершена', partial: 'Получены частичные данные', stale: 'Данные устарели', suspended: 'Синхронизация приостановлена', failed: 'Синхронизация завершилась с ошибкой', revoked: 'Доступ отозван', reconciliation_required: 'Требуется сверка',
};
const operationCopy: Record<Operation, string> = {
  enumerate_mailbox: 'Просмотр состава почтового ящика', enumerate_folder_uids: 'Перечисление сообщений в одобренных папках', read_message_metadata: 'Чтение метаданных сообщения', read_raw_message: 'Чтение исходного сообщения', read_attachment: 'Чтение вложений', synchronize: 'Синхронизация', observe_folder_generation: 'Проверка поколения папки',
};
const capabilityCopy: Partial<Record<Capability, string>> = {
  enumeration: 'Перечисление объектов', metadata_read: 'Чтение метаданных', binary_read: 'Чтение файлов', tabular_read: 'Чтение таблиц', structured_read: 'Чтение структурированных данных', stable_identity: 'Стабильная идентичность', incremental_change_discovery: 'Инкрементальные изменения', full_reconciliation: 'Полная сверка', lifecycle_evidence: 'Доказательства жизненного цикла', health_diagnostics: 'Безопасная диагностика', crm: 'CRM-связи', mailbox_enumeration: 'Перечисление почты', message_metadata_read: 'Чтение метаданных сообщений', raw_message_read: 'Чтение сообщений', attachment_read: 'Чтение вложений',
};
const capabilityStatusCopy: Record<components['schemas']['ProviderCapabilityStatus'], string> = {
  supported: 'Поддерживается', unsupported: 'Не поддерживается', unqualified: 'Не квалифицировано',
};
const providerPresentationCopy: Record<ProviderKind, ProviderPresentation> = {
  google: { family: 'Google Workspace', label: 'Google Drive / Sheets', description: 'Файлы и таблицы из уже одобренной области Google Workspace.' },
  yandex_disk: { family: 'Яндекс 360', label: 'Yandex Disk', description: 'Файлы из одобренной read-only области Yandex Disk.' },
  bitrix24: { family: 'Bitrix24', label: 'Bitrix24 Disk / CRM', description: 'Разрешённые Disk-файлы и проверяемые связи с CRM-материалами.' },
  mounted_filesystem: { family: 'Network storage', label: 'Mounted filesystem', description: 'Файлы из настроенного read-only корня смонтированного хранилища.' },
  yandex_mail: { family: 'Яндекс 360', label: 'Yandex Mail', description: 'Почтовые сообщения через одобренное read-only подключение Яндекс 360.' },
};
type OperatorDiagnosticKind = 'healthy' | 'partial' | 'stale' | 'degraded' | 'revoked' | 'reconciliation_required';
interface OperatorDiagnostic { guidance: string; kind: OperatorDiagnosticKind; title: string }
const operatorDiagnosticCopy: Record<OperatorDiagnosticKind, OperatorDiagnostic> = {
  healthy: { kind: 'healthy', title: 'Синхронизация работает штатно', guidance: 'Дополнительные действия не требуются. Обновите статус, если ожидаемые данные не появляются.' },
  partial: { kind: 'partial', title: 'Получены частичные данные', guidance: 'Не считайте выборку полной. Обновите статус после следующей попытки синхронизации.' },
  stale: { kind: 'stale', title: 'Данные устарели', guidance: 'Сверьте время последней полной синхронизации перед использованием данных и обновите статус.' },
  degraded: { kind: 'degraded', title: 'Работает с ограничениями', guidance: 'Не расширяйте область доступа. Обновите статус позднее или следуйте операционному регламенту.' },
  revoked: { kind: 'revoked', title: 'Доступ отозван', guidance: 'Остановите ожидание синхронизации и запросите новое одобрение подключения у владельца доступа.' },
  reconciliation_required: { kind: 'reconciliation_required', title: 'Требуется сверка', guidance: 'Не запускайте ручной импорт. Дождитесь безопасной полной сверки и затем обновите статус.' },
};

export function ProviderConnectionsPage() {
  const auth = useAuth();
  const { scope } = useScope();
  const pal = usePal();
  const [searchParams, setSearchParams] = useSearchParams();
  const client = useMemo(() => createScopedApiClient({ baseUrl: window.location.origin, getBearerCredential: auth.getBearerCredential ?? (() => ''), scope }), [auth.getBearerCredential, scope]);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ConfigurationDraft>(emptyConfigurationDraft);
  const [configurationOpen, setConfigurationOpen] = useState(false);
  const [receipt, setReceipt] = useState<{ commandId: string; replay: boolean } | null>(null);
  const commandKey = useRef<string | null>(null);
  const previousScope = useRef(`${scope.workspaceId}/${scope.environmentId}`);
  const selectedId = searchParams.get('connection');

  const collectionKey = ['provider-connections', scope.workspaceId, scope.environmentId] as const;
  const collectionQuery = useInfiniteQuery({
    queryKey: collectionKey,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) => {
      const { data } = await client.GET('/api/v1/source-connections', { params: { query: { limit: 50, ...(pageParam ? { cursor: pageParam } : {}) } }, signal });
      return data ?? { items: [], next_cursor: null };
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
  });
  const collectionItems = collectionQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const inspectionQueries = useQueries({
    queries: collectionItems.map((item) => ({
      queryKey: [...collectionKey, item.connection_id],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const { data } = await client.GET('/api/v1/source-connections/{connection_id}', { params: { path: { connection_id: item.connection_id } }, signal });
        return data?.data ?? item;
      },
      initialData: item,
      retry: false,
    })),
  });
  const items = collectionItems.map((item, index) => inspectionQueries[index]?.data ?? item);
  const selected = items.find((item) => item.connection_id === selectedId) ?? null;
  const detailFailure = inspectionQueries.some((query) => query.isError);
  const refreshing = collectionQuery.isFetching || inspectionQueries.some((query) => query.isFetching);
  const permissionDenied = [collectionQuery.error, ...inspectionQueries.map((query) => query.error)].some((failure) => failure instanceof SpineApiError && failure.status === 403);

  const configureMutation = useMutation({
    mutationFn: async () => {
      const idempotencyKey = commandKey.current ?? crypto.randomUUID();
      commandKey.current = idempotencyKey;
      const { data } = await client.POST('/api/v1/source-connections', {
        params: { header: { 'Idempotency-Key': idempotencyKey } },
        body: { approval_id: draft.approvalId, connection_id: draft.connectionId, expected_version: draft.expectedVersion, expected_predecessor_version_id: draft.predecessorVersionId },
      });
      if (!data?.result) throw new Error('missing connection result');
      return data;
    },
    onSuccess: async (data) => {
      setReceipt({ commandId: data.command_id, replay: data.replay });
      commandKey.current = null;
      setConfigurationOpen(false);
      setDraft((current) => ({ ...current, expectedVersion: 0, predecessorVersionId: null }));
      await queryClient.invalidateQueries({ queryKey: collectionKey });
    },
    onError: (failure) => {
      setReceipt(null);
      if (failure instanceof SpineApiError && (failure.code === 'spine.persistence.optimistic_conflict' || failure.code === 'spine.persistence.idempotency_conflict' || [404, 422].includes(failure.status))) commandKey.current = null;
    },
    retry: false,
  });

  useEffect(() => {
    const nextScope = `${scope.workspaceId}/${scope.environmentId}`;
    if (previousScope.current === nextScope) return;
    previousScope.current = nextScope;
    setSearchParams((current) => {
      const nextParams = new URLSearchParams(current);
      nextParams.delete('connection');
      nextParams.set('workspace', scope.workspaceId);
      nextParams.set('environment', scope.environmentId);
      return nextParams;
    }, { replace: true });
    setConfigurationOpen(false); setDraft(emptyConfigurationDraft); setReceipt(null); commandKey.current = null; configureMutation.reset();
  // Workspace/environment is a security boundary; local references must not cross it.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope.workspaceId, scope.environmentId]);

  useEffect(() => {
    if (!configurationOpen || !selected || draft.connectionId !== selected.connection_id) return;
    setDraft((current) => ({ ...current, expectedVersion: selected.version, predecessorVersionId: selected.connection_version_id }));
  }, [configurationOpen, draft.connectionId, selected]);

  const themeVariables = {
    '--connections-bg': pal.bg, '--connections-surface': pal.bgElevated, '--connections-subtle': pal.bgSubtle, '--connections-muted': pal.bgMuted, '--connections-border': pal.borderSubtle, '--connections-border-strong': pal.border, '--connections-text': pal.text, '--connections-secondary': pal.textSecondary, '--connections-tertiary': pal.textTertiary, '--connections-accent-bg': pal.accentBg, '--connections-accent': pal.accentText, '--connections-success': pal.success, '--connections-success-bg': pal.successBg, '--connections-warning': pal.warning, '--connections-warning-bg': pal.warningBg, '--connections-danger': pal.danger, '--connections-danger-bg': pal.dangerBg, '--connections-shadow': pal.shadow,
  } as CSSProperties;

  if (!auth.getBearerCredential || permissionDenied) {
    return <section className={styles.page} style={themeVariables}><ConnectionState action={null} alert description="Недостаточно прав для просмотра подключений источников в выбранной области. Защищённые сведения не раскрыты." title="Доступ запрещён" /></section>;
  }

  async function configure(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (draft.approvalId && draft.connectionId) await configureMutation.mutateAsync().catch(() => undefined);
  }
  function openConfiguration(item?: Connection) {
    setReceipt(null); configureMutation.reset(); commandKey.current = null;
    setDraft({ approvalId: item?.approval_id ?? '', connectionId: item?.connection_id ?? '', expectedVersion: item?.version ?? 0, predecessorVersionId: item?.connection_version_id ?? null }); setConfigurationOpen(true);
  }
  function selectConnection(connection: string | null) {
    setSearchParams((current) => {
      const nextParams = new URLSearchParams(current);
      if (connection) nextParams.set('connection', connection);
      else nextParams.delete('connection');
      return nextParams;
    });
  }

  const mutationError = configureMutation.error;
  const error = mutationError instanceof SpineApiError && mutationError.code === 'spine.persistence.optimistic_conflict'
    ? 'Версия подключения уже изменилась. Обновите безопасный статус и повторите команду.'
    : mutationError instanceof SpineApiError && mutationError.code === 'spine.persistence.idempotency_conflict'
      ? 'Ключ повтора уже использован с другими ссылками. Отправьте новую команду.'
      : mutationError instanceof SpineApiError && [404, 422].includes(mutationError.status)
        ? 'Approval недействителен или недоступен в этой области. Укажите зарегистрированные сервером ссылки.'
        : mutationError ? 'Не удалось сохранить конфигурацию. Повтор отправит ту же идемпотентную команду.'
          : collectionQuery.isError ? 'Не удалось загрузить подключения источников. Защищённые сведения не раскрыты.' : null;

  return (
    <section aria-labelledby="provider-connections-heading" className={styles.page} style={themeVariables}>
      <header className={styles.header}>
        <div><p className={styles.eyebrow}>Управление источниками</p><h2 id="provider-connections-heading">Подключения источников</h2><p>Файлы, таблицы, CRM-материалы и почта в пределах уже одобренной конфигурации.</p></div>
        <div className={styles.headerActions}>
          <button className={styles.secondaryButton} disabled={refreshing} onClick={() => void queryClient.invalidateQueries({ queryKey: collectionKey })} type="button"><RefreshCw aria-hidden="true" size={14} />{refreshing ? 'Обновляем…' : 'Обновить статус'}</button>
          <button className={styles.primaryButton} onClick={() => openConfiguration()} type="button"><Plus aria-hidden="true" size={15} />Настроить по ссылкам</button>
        </div>
      </header>
      <div className={styles.governanceNote}><ShieldCheck aria-hidden="true" size={18} /><div><strong>Только одобренные ссылки</strong><span>Здесь нельзя выдать OAuth consent, принять учётные данные или расширить область ресурсов.</span></div></div>

      {configurationOpen ? <ConfigurationForm draft={draft} error={error} pending={configureMutation.isPending} onChange={(change) => { setDraft((current) => ({ ...current, ...change })); commandKey.current = null; }} onCancel={() => { setConfigurationOpen(false); configureMutation.reset(); }} onSubmit={configure} /> : null}
      {receipt ? <p className={styles.success} role="status"><Check aria-hidden="true" size={16} />{receipt.replay ? 'Повтор команды распознан: сохранённый результат возвращён без второго эффекта.' : 'Одобренная конфигурация сохранена.'}<code>{receipt.commandId}</code></p> : null}
      {!configurationOpen && error ? <p className={styles.error} role="alert">{error}</p> : null}
      {collectionQuery.isPending ? <LoadingCatalog /> : null}
      {refreshing && items.length > 0 ? <p className={styles.notice} role="status">Обновляем данные; показано последнее безопасное состояние.</p> : null}
      {detailFailure && items.length > 0 ? <p className={styles.error} role="alert">Часть актуальных проверок недоступна; показано последнее безопасное состояние.</p> : null}
      {!collectionQuery.isPending && !collectionQuery.isError && items.length === 0 ? <ConnectionState action={<button className={styles.primaryButton} onClick={() => openConfiguration()} type="button"><Plus aria-hidden="true" size={15} />Настроить по ссылкам</button>} description="В этой рабочей области нет подключений с одобренной областью ресурсов." title="Подключения источников не настроены" /> : null}
      {!collectionQuery.isPending && items.length > 0 ? (selected ? <ConnectionDetails item={selected} onBack={() => selectConnection(null)} onConfigure={() => openConfiguration(selected)} /> : <div aria-label="Одобренные подключения источников" className={styles.catalog} role="list">{items.map((item) => <ConnectionCard item={item} key={item.connection_id} onOpen={() => selectConnection(item.connection_id)} />)}</div>) : null}
      {collectionQuery.hasNextPage && !selected ? <button className={styles.secondaryButton} disabled={collectionQuery.isFetchingNextPage} onClick={() => void collectionQuery.fetchNextPage()} type="button">{collectionQuery.isFetchingNextPage ? 'Загружаем ещё…' : 'Показать ещё'}</button> : null}
    </section>
  );
}

function ConfigurationForm({ draft, error, onCancel, onChange, onSubmit, pending }: { draft: ConfigurationDraft; error: string | null; onCancel: () => void; onChange: (change: Partial<ConfigurationDraft>) => void; onSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>; pending: boolean }) {
  return <form aria-labelledby="connection-configuration-heading" className={styles.configuration} onSubmit={(event) => void onSubmit(event)}>
    <div className={styles.configurationHeader}><div><p className={styles.eyebrow}>{draft.expectedVersion > 0 ? 'Новая версия конфигурации' : 'Одобренное подключение'}</p><h3 id="connection-configuration-heading">Сохранить серверные ссылки</h3><p>Отправляются только идентификаторы approval и логического подключения.</p></div><button aria-label="Закрыть форму конфигурации" className={styles.iconButton} onClick={onCancel} type="button"><X aria-hidden="true" size={16} /></button></div>
    <div className={styles.fields}><label><span>Approval ID <small>одобрение Security/Data Owner</small></span><input autoComplete="off" required value={draft.approvalId} onChange={(event) => onChange({ approvalId: event.target.value.trim() })} /></label><label><span>Connection ID <small>логическое подключение</small></span><input autoComplete="off" required value={draft.connectionId} onChange={(event) => onChange({ connectionId: event.target.value.trim() })} /></label></div>
    {draft.expectedVersion > 0 ? <div className={styles.versionFence}><span>Текущая версия: <b>{draft.expectedVersion}</b></span><span>Точная версия подключения: <code>{draft.predecessorVersionId}</code></span></div> : null}
    <div className={styles.safetyLine}><LockKeyhole aria-hidden="true" size={14} />OAuth, токены, адрес ящика и произвольные папки не принимаются.</div>
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    <div className={styles.formActions}><button className={styles.secondaryButton} onClick={onCancel} type="button">Отмена</button><button className={styles.primaryButton} disabled={pending || !draft.approvalId || !draft.connectionId} type="submit">{pending ? 'Сохраняем…' : 'Сохранить ссылки'}</button></div>
  </form>;
}

function ConnectionCard({ item, onOpen }: { item: Connection; onOpen: () => void }) {
  const sync = item.mailbox_sync;
  const lag = sync ? sync.lag_seconds : item.cursor_lag_seconds;
  const diagnostic = operatorDiagnostic(item);
  const presentation = providerPresentation(item);
  return <article className={styles.card} data-health={item.health} role="listitem"><div className={styles.cardTop}><span className={styles.providerIcon}><Mail aria-hidden="true" size={18} /></span><span className={styles.statusBadge} data-state={diagnostic.kind}>{diagnostic.title}</span></div><div><p className={styles.providerKind}>{presentation.family}</p><h3>{presentation.label}</h3><p className={styles.cardDescription}>{presentation.description}</p></div><dl className={styles.cardMetrics}><div><dt>Версия</dt><dd>v{item.version}</dd></div>{lag !== null && lag !== undefined ? <div><dt>Отставание</dt><dd>{formatLag(lag)}</dd></div> : null}<div><dt>Возможности</dt><dd>{item.capabilities.length}</dd></div></dl><button aria-label={`Открыть ${presentation.label}`} className={styles.cardAction} onClick={onOpen} type="button">Открыть безопасный статус <ChevronRight aria-hidden="true" size={14} /></button></article>;
}

function ConnectionDetails({ item, onBack, onConfigure }: { item: Connection; onBack: () => void; onConfigure: () => void }) {
  const sync = item.mailbox_sync;
  const lastCompleteAt = sync ? sync.last_complete_at : item.last_successful_reconciliation_at;
  const lag = sync ? sync.lag_seconds : item.cursor_lag_seconds;
  const diagnostic = operatorDiagnostic(item);
  const unsupported = item.capabilities.filter((capability) => capability.status !== 'supported');
  const presentation = providerPresentation(item);
  return <article aria-labelledby="connection-detail-heading" className={styles.details}>
    <header className={styles.detailHeader}><button className={styles.backButton} onClick={onBack} type="button"><ChevronLeft aria-hidden="true" size={15} />Все коннекторы</button><div className={styles.detailTitle}><span className={styles.providerIcon}><Mail aria-hidden="true" size={18} /></span><div><p className={styles.providerKind}>{presentation.family}</p><h3 id="connection-detail-heading">{presentation.label}</h3></div></div><button className={styles.secondaryButton} onClick={onConfigure} type="button">Изменить конфигурацию</button></header>
    <div className={styles.detailGrid}>
      <section aria-labelledby="safe-status-heading" className={styles.detailSection}><div className={styles.sectionHeading}><div><p className={styles.eyebrow}>Наблюдаемое состояние</p><h4 id="safe-status-heading">Безопасный статус</h4></div><span className={styles.statusBadge} data-state={diagnostic.kind}>{diagnostic.title}</span></div><div aria-label={diagnostic.title} className={styles.operatorDiagnostic} data-state={diagnostic.kind} role="status"><CircleAlert aria-hidden="true" size={17} /><div><strong>{diagnostic.title}</strong><p>{diagnostic.guidance}</p></div></div><Definition label="Состояние подключения">{healthCopy[item.health]}</Definition>{lastCompleteAt ? <Definition label={sync ? 'Последняя полная синхронизация' : 'Последняя успешная сверка'}>{formatDate(lastCompleteAt)}</Definition> : null}{lag !== null && lag !== undefined ? <Definition label="Отставание">{formatLag(lag)}</Definition> : null}<Definition label="Проверено">{formatDate(item.observed_at)}</Definition>{item.diagnostic_codes.length > 0 ? <Definition label="Безопасные коды"><span className={styles.diagnosticCodes}>{item.diagnostic_codes.map((code) => <code key={code}>{code}</code>)}</span></Definition> : null}{unsupported.length > 0 ? <p className={styles.warning} role="status">{unsupported.length} возможностей недоступны или ещё не квалифицированы.</p> : null}</section>
      <section aria-labelledby="approval-heading" className={styles.detailSection}><p className={styles.eyebrow}>Версионированная конфигурация</p><h4 id="approval-heading">Одобрение и версия</h4><Definition label="Approval ID"><code>{item.approval_id}</code></Definition><Definition label="Connection ID"><code>{item.connection_id}</code></Definition><Definition label="Точная версия подключения"><code>{item.connection_version_id}</code></Definition><Definition label="Версия конфигурации">{item.version}</Definition></section>
      <section aria-labelledby="scope-heading" className={styles.detailSection}><p className={styles.eyebrow}>Доступ провайдера</p><h4 id="scope-heading">Одобренная область</h4><div className={styles.referenceList}>{item.approved_resource_scope_references.map((reference) => <code key={reference}>{reference}</code>)}</div><p className={styles.protectedNote}><LockKeyhole aria-hidden="true" size={13} />Защищённые названия ресурсов и содержимое не отображаются.</p></section>
      <section aria-labelledby="operations-heading" className={styles.detailSection}><p className={styles.eyebrow}>Границы чтения</p><h4 id="operations-heading">Разрешённые операции</h4>{item.permitted_operations.length > 0 ? <ul className={styles.operationList}>{item.permitted_operations.map((operation) => <li key={operation}><Check aria-hidden="true" size={14} /><span>{operationCopy[operation]}</span><code>{operation}</code></li>)}</ul> : <p>Для этого типа источника операции выражены через квалифицированные возможности.</p>}<p className={styles.protectedNote}><ChevronRight aria-hidden="true" size={13} />Исходящие и изменяющие операции не разрешены.</p></section>
      <section aria-labelledby="capabilities-heading" className={`${styles.detailSection} ${styles.fullWidth}`}><p className={styles.eyebrow}>UI qualification</p><h4 id="capabilities-heading">Возможности подключения</h4><div className={styles.qualificationNote}><ShieldCheck aria-hidden="true" size={16} /><div><p>Этот экран квалифицирует только отображение безопасного API-контракта.</p><p>Он не подтверждает live-доступ или работоспособность внешнего провайдера.</p></div></div><ul className={styles.capabilityList}>{item.capabilities.map((capability) => <li data-status={capability.status} key={capability.kind}><span><b>{capabilityCopy[capability.kind] ?? capability.kind}</b><code>{capability.kind}</code></span><span>{capabilityStatusCopy[capability.status]}</span></li>)}</ul></section>
    </div>
  </article>;
}

function Definition({ children, label }: { children: ReactNode; label: string }) { return <dl className={styles.definition}><dt>{label}</dt><dd>{children}</dd></dl>; }
function LoadingCatalog() { return <div aria-label="Загрузка подключений источников" aria-live="polite" className={styles.loading} role="status"><span>Загружаем подключения источников…</span>{[0, 1].map((item) => <div aria-hidden="true" className={styles.skeleton} key={item} />)}</div>; }
function ConnectionState({ action, alert = false, description, title }: { action: ReactNode; alert?: boolean; description: string; title: string }) { return <div aria-label={title} className={styles.state} role={alert ? 'alert' : 'region'}><span className={styles.stateIcon}><Mail aria-hidden="true" size={22} /></span><h3>{title}</h3><p>{description}</p>{action}</div>; }
function operatorDiagnostic(item: Connection): OperatorDiagnostic {
  const syncState = item.mailbox_sync?.state;
  if (item.health === 'revoked' || syncState === 'revoked') return operatorDiagnosticCopy.revoked;
  if (item.health === 'reconciliation_required' || syncState === 'reconciliation_required') return operatorDiagnosticCopy.reconciliation_required;
  if (syncState === 'partial') return operatorDiagnosticCopy.partial;
  if (syncState === 'stale') return operatorDiagnosticCopy.stale;
  if (syncState === 'failed' || syncState === 'suspended') return { ...operatorDiagnosticCopy.degraded, title: syncCopy[syncState] };
  if (item.health === 'degraded') return operatorDiagnosticCopy.degraded;
  if (syncState) return { ...operatorDiagnosticCopy.healthy, title: syncCopy[syncState] };
  return { ...operatorDiagnosticCopy.healthy, title: healthCopy[item.health] };
}
function isGoogleMailbox(item: Connection): boolean { return item.provider_kind === 'google' && (item.mailbox_sync !== null || item.permitted_operations.length > 0); }
function providerPresentation(item: Connection): ProviderPresentation {
  if (isGoogleMailbox(item)) {
    return { family: 'Google Workspace', label: 'Gmail', description: 'Входящая почта и вложения из уже одобренного Google Workspace подключения.' };
  }
  return providerPresentationCopy[item.provider_kind];
}
function formatDate(value: string | null | undefined): string { return value ? new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(new Date(value)) : 'Нет завершённой синхронизации'; }
function formatLag(value: number | null | undefined): string { if (value === null || value === undefined) return 'Не вычисляется'; if (value < 60) return `${value} сек`; if (value < 3600) return `${Math.ceil(value / 60)} мин`; const hours = Math.floor(value / 3600); const minutes = Math.floor((value % 3600) / 60); return `${hours} ч${minutes ? ` ${minutes} мин` : ''}`; }

export { healthCopy, operationCopy, syncCopy };
