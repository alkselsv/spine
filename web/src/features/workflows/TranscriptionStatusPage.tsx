import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useParams } from 'react-router';

import { createScopedApiClient } from '../../api/client';
import { apiQueryOptions } from '../../api/query';
import { useAuth } from '../../app/auth';
import { ApiErrorPanel, StatusPanel } from '../../app/status';
import { useScope } from '../../app/scope';
import {
  isValidTranscriptionJobId,
  loadTranscriptionStatus,
  type TranscriptionStatus,
} from './transcription-status';
import styles from './TranscriptionStatusPage.module.css';

const copy = {
  title: 'Статус расшифровки',
  description: 'Состояние обработки аудио без содержимого записи и расшифровки.',
  refresh: 'Обновить статус',
  unavailable: 'Статус недоступен',
  unavailableDescription: 'Задание не найдено или доступ к нему не подтверждён.',
  states: {
    pending: 'Расшифровка выполняется',
    completed: 'Расшифровка завершена',
    partial: 'Получен частичный результат',
    failed: 'Расшифровка завершилась ошибкой',
    cancelled: 'Расшифровка отменена',
    unavailable: 'Статус недоступен',
    permission_denied: 'Доступ запрещён',
  },
} as const;

export function TranscriptionStatusPage() {
  const auth = useAuth();
  const { scope, queryKey } = useScope();
  const { jobId = '' } = useParams();
  const client = useMemo(() => createScopedApiClient({
    baseUrl: window.location.origin,
    getBearerCredential: auth.getBearerCredential ?? (() => ''),
    scope,
  }), [auth.getBearerCredential, scope]);
  const statusQuery = useQuery({
    ...apiQueryOptions({
      load: (signal) => loadTranscriptionStatus(client, jobId, signal, scope),
      queryKey: [...queryKey('transcription-status'), jobId],
    }),
    enabled: Boolean(auth.getBearerCredential && isValidTranscriptionJobId(jobId)),
  });

  if (!auth.getBearerCredential) return <StatusPanel state="permissionDenied" />;
  if (!isValidTranscriptionJobId(jobId)) return <StatusPanel state="failed" />;
  if (statusQuery.isPending) return <StatusPanel state="loading" />;
  if (statusQuery.error) {
    return (
      <section className={styles.page} aria-labelledby="transcription-status-heading">
        <Header />
        <ApiErrorPanel error={statusQuery.error} />
        <RefreshButton onRefresh={() => void statusQuery.refetch()} />
      </section>
    );
  }
  if (!statusQuery.data) return <StatusPanel state="failed" />;

  return (
    <TranscriptionStatusView
      onRefresh={() => void statusQuery.refetch()}
      status={statusQuery.data}
    />
  );
}

function TranscriptionStatusView({
  onRefresh,
  status,
}: {
  onRefresh: () => void;
  status: TranscriptionStatus;
}) {
  const hidden = status.status === 'unavailable' || status.status === 'permission_denied';
  return (
    <section className={styles.page} aria-labelledby="transcription-status-heading">
      <Header />
      <div className={styles.statusRow} role="status">
        <span className={`${styles.statusMark} ${styles[`statusMark_${status.status}`]}`} aria-hidden="true" />
        <div>
          <h3>{hidden ? copy.unavailable : copy.states[status.status]}</h3>
          {hidden ? <p>{copy.unavailableDescription}</p> : null}
        </div>
      </div>
      {status.status === 'partial' ? <StatusPanel state="partial" /> : null}
      {!hidden ? <ProcessingDetails status={status} /> : null}
      <RefreshButton onRefresh={onRefresh} />
    </section>
  );
}

function Header() {
  return (
    <header className={styles.header}>
      <p className={styles.eyebrow}>Обработка встреч</p>
      <h2 id="transcription-status-heading">{copy.title}</h2>
      <p>{copy.description}</p>
    </header>
  );
}

function ProcessingDetails({ status }: { status: TranscriptionStatus }) {
  return (
    <section className={styles.card} aria-labelledby="transcription-details-heading">
      <h3 id="transcription-details-heading">Безопасные сведения об обработке</h3>
      <dl className={styles.details}>
        <Value label="Задание" value={status.job_id} code />
        <Value label="Источник" value={status.source_object_id} code />
        <Value label="Ревизия источника" value={status.source_revision_id} code />
        <Value label="Профиль обработки" value={status.processing_profile_reference} />
        <Value label="ASR-адаптер" value={status.adapter_reference} />
        <Value label="Конфигурация модели" value={status.model_config_reference} />
        <Value label="Создано" value={formatTimestamp(status.created_at)} />
        <Value label="Завершено" value={formatTimestamp(status.completed_at)} />
        <Value label="Код результата" value={status.terminal_code} code />
        <Value label="Можно повторить" value={status.retryable == null ? null : status.retryable ? 'Да' : 'Нет'} />
        <Value label="Запрошена отмена" value={status.cancellation_requested == null ? null : status.cancellation_requested ? 'Да' : 'Нет'} />
      </dl>
    </section>
  );
}

function Value({ label, value, code = false }: { label: string; value?: string | null; code?: boolean }) {
  if (!value) return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>{code ? <code>{value}</code> : value}</dd>
    </div>
  );
}

function RefreshButton({ onRefresh }: { onRefresh: () => void }) {
  return <button className={styles.refresh} onClick={onRefresh} type="button">{copy.refresh}</button>;
}

function formatTimestamp(value?: string | null): string | null {
  if (!value) return null;
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value));
}
