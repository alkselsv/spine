import { useInfiniteQuery, useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState, type FormEvent } from 'react';

import type { components } from '../../../api/generated/schema';
import { createScopedApiClient, SpineApiError } from '../../api/client';
import { useAuth } from '../../app/auth';
import { useScope } from '../../app/scope';
import styles from './ProviderConnectionsPage.module.css';

type Connection = components['schemas']['ProviderConnectionResponse'];
type Health = Connection['health'];

const healthCopy: Record<Health, string> = {
  healthy: 'Подключение исправно',
  degraded: 'Подключение работает с ограничениями',
  revoked: 'Доступ отозван',
  reconciliation_required: 'Требуется сверка',
};

export function ProviderConnectionsPage() {
  const auth = useAuth();
  const { scope } = useScope();
  const client = useMemo(
    () => createScopedApiClient({
      baseUrl: window.location.origin,
      getBearerCredential: auth.getBearerCredential ?? (() => ''),
      scope,
    }),
    [auth.getBearerCredential, scope],
  );
  const queryClient = useQueryClient();
  const [approvalId, setApprovalId] = useState('');
  const [connectionId, setConnectionId] = useState('');
  const [expectedVersion, setExpectedVersion] = useState(0);
  const [predecessorVersionId, setPredecessorVersionId] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<string | null>(null);
  const commandKey = useRef<string | null>(null);

  const collectionKey = ['provider-connections', scope.workspaceId, scope.environmentId] as const;
  const collectionQuery = useInfiniteQuery({
    queryKey: collectionKey,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }) => {
      const { data } = await client.GET('/api/v1/source-connections', {
        params: { query: { limit: 50, ...(pageParam ? { cursor: pageParam } : {}) } },
        signal,
      });
      return data ?? { items: [], next_cursor: null };
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const collectionItems = collectionQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const inspectionQueries = useQueries({
    queries: collectionItems.map((item) => ({
      queryKey: [...collectionKey, item.connection_id],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const { data } = await client.GET('/api/v1/source-connections/{connection_id}', {
          params: { path: { connection_id: item.connection_id } },
          signal,
        });
        return data?.data ?? item;
      },
      initialData: item,
    })),
  });
  const items = collectionItems.map((item, index) => inspectionQueries[index]?.data ?? item);
  const detailFailure = inspectionQueries.some((query) => query.isError);
  const refreshing = collectionQuery.isFetching || inspectionQueries.some((query) => query.isFetching);
  const permissionDenied = [collectionQuery.error, ...inspectionQueries.map((query) => query.error)]
    .some((failure) => failure instanceof SpineApiError && failure.status === 403);

  const configureMutation = useMutation({
    mutationFn: async () => {
      const idempotencyKey = commandKey.current ?? crypto.randomUUID();
      commandKey.current = idempotencyKey;
      const { data } = await client.POST('/api/v1/source-connections', {
        params: { header: { 'Idempotency-Key': idempotencyKey } },
        body: {
          approval_id: approvalId,
          connection_id: connectionId,
          expected_version: expectedVersion,
          expected_predecessor_version_id: predecessorVersionId,
        },
      });
      if (!data?.result) throw new Error('missing connection result');
      return data;
    },
    onSuccess: async (data) => {
      setReceipt(data.command_id);
      commandKey.current = null;
      setExpectedVersion(0);
      setPredecessorVersionId(null);
      await queryClient.invalidateQueries({ queryKey: collectionKey });
    },
    onError: (failure) => {
      setReceipt(null);
      if (failure instanceof SpineApiError && failure.code === 'spine.persistence.idempotency_conflict') {
        commandKey.current = null;
      }
    },
  });

  if (!auth.getBearerCredential || permissionDenied) {
    return <ConnectionState title="Доступ запрещён" description="Недостаточно прав для просмотра подключений в выбранной области." alert />;
  }

  async function configure(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!approvalId || !connectionId) return;
    await configureMutation.mutateAsync().catch(() => undefined);
  }

  const mutationError = configureMutation.error;
  const error = mutationError instanceof SpineApiError && mutationError.code === 'spine.persistence.optimistic_conflict'
    ? 'Конфигурация уже изменилась. Обновите список перед созданием новой версии.'
    : mutationError instanceof SpineApiError && mutationError.code === 'spine.persistence.idempotency_conflict'
      ? 'Эта команда уже использована с другими параметрами. Создайте новую команду.'
      : mutationError
        ? 'Не удалось создать версию подключения. Повторите ту же команду.'
        : collectionQuery.isError
          ? 'Не удалось получить состояние подключений. Повторите запрос.'
          : null;

  return (
    <section aria-labelledby="provider-connections-heading" className={styles.page}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>Источники</p>
          <h2 id="provider-connections-heading">Подключения провайдеров</h2>
          <p>Версионированные ссылки на уже одобренные области. Эта форма не создаёт согласование доступа.</p>
        </div>
        <button disabled={refreshing} onClick={() => void queryClient.invalidateQueries({ queryKey: collectionKey })} type="button">Обновить</button>
      </header>

      <form className={styles.form} onSubmit={(event) => void configure(event)}>
        <label>
          Approval ID
          <input required value={approvalId} onChange={(event) => { setApprovalId(event.target.value.trim()); commandKey.current = null; }} />
        </label>
        <label>
          Connection ID
          <input required value={connectionId} onChange={(event) => { setConnectionId(event.target.value.trim()); commandKey.current = null; }} />
        </label>
        <button disabled={configureMutation.isPending || !approvalId || !connectionId} type="submit">
          {configureMutation.isPending ? 'Создаём…' : expectedVersion > 0 ? 'Создать новую версию' : 'Создать версию'}
        </button>
      </form>

      {receipt ? <p className={styles.receipt} role="status">Версия создана. Квитанция: {receipt}</p> : null}
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {collectionQuery.isPending ? <p role="status">Загружаем подключения…</p> : null}
      {refreshing && items.length > 0 ? <p role="status">Обновляем данные; показано последнее сохранённое состояние.</p> : null}
      {detailFailure && items.length > 0 ? <p className={styles.error} role="alert">Часть актуальных проверок недоступна; показано сохранённое состояние.</p> : null}
      {!collectionQuery.isPending && !error && items.length === 0 ? (
        <ConnectionState title="Подключения не настроены" description="Укажите идентификаторы существующего approval и логического подключения." />
      ) : null}
      {!collectionQuery.isPending && items.length > 0 ? (
        <div className={styles.grid}>
          {items.map((item) => <ConnectionCard item={item} key={item.connection_id} onNewVersion={() => {
            setConnectionId(item.connection_id);
            setExpectedVersion(item.version);
            setPredecessorVersionId(item.connection_version_id);
            setReceipt(null);
            commandKey.current = null;
          }} />)}
        </div>
      ) : null}
      {collectionQuery.hasNextPage ? (
        <button
          disabled={collectionQuery.isFetchingNextPage}
          onClick={() => void collectionQuery.fetchNextPage()}
          type="button"
        >
          {collectionQuery.isFetchingNextPage ? 'Загружаем ещё…' : 'Показать ещё'}
        </button>
      ) : null}
    </section>
  );
}

function ConnectionCard({ item, onNewVersion }: { item: Connection; onNewVersion: () => void }) {
  return (
    <article className={styles.card} data-health={item.health}>
      <header>
        <div>
          <span className={styles.provider}>{item.provider_kind}</span>
          <h3>{healthCopy[item.health]}</h3>
        </div>
        <span className={styles.version}>v{item.version}</span>
      </header>
      <dl>
        <div><dt>Connection ID</dt><dd>{item.connection_id}</dd></div>
        <div><dt>Статус</dt><dd>{item.status}</dd></div>
        <div><dt>Поколение авторизации</dt><dd>{item.authorization_generation}</dd></div>
        <div><dt>Отставание cursor</dt><dd>{item.cursor_lag_seconds ?? 'Нет данных'}</dd></div>
      </dl>
      <section aria-label="Возможности подключения">
        <h4>Возможности</h4>
        <ul>
          {item.capabilities.map((capability) => (
            <li key={capability.kind}>
              <code>{capability.kind}</code>
              <span>{capability.status}</span>
            </li>
          ))}
        </ul>
      </section>
      <section aria-label="Безопасная диагностика">
        <h4>Безопасная диагностика</h4>
        <p>{item.diagnostic_codes.join(', ') || 'Сигналов нет'}</p>
      </section>
      <button onClick={onNewVersion} type="button">Новая версия</button>
    </article>
  );
}

function ConnectionState({
  alert = false,
  description,
  title,
}: {
  alert?: boolean;
  description: string;
  title: string;
}) {
  return (
    <div className={styles.state} role={alert ? 'alert' : 'region'} aria-label={title}>
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  );
}

export { healthCopy };
