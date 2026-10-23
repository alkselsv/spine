import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { SpineApiError, type ScopedApiClient } from '../../api/client';
import { ApiErrorPanel } from '../../app/status';
import { useScope } from '../../app/scope';
import { formatBytes } from '../../i18n/format';
import { ru } from '../../i18n/resources';
import {
  applyBatchItems,
  createIdempotencyKey,
  createUploadRows,
  DOCUMENT_UPLOAD_ACCEPT,
  type UploadRow,
  validateUploadFiles,
  type UploadValidationIssue,
  type UploadValidationResult,
} from './upload';
import { loadBatchStatus, submitDocumentBatch, type BatchStatusResponse } from './upload-api';
import styles from './DocumentsPage.module.css';

const POLL_INTERVAL_MS = 750;

interface UploadSession {
  idempotencyKey: string;
  rows: UploadRow[];
  submitted: boolean;
  replay: boolean;
  runId?: string;
  cancelled: boolean;
  error?: unknown;
}

export function DocumentUpload({
  client,
  disabled = false,
  scopeSearch,
}: {
  client: ScopedApiClient;
  disabled?: boolean;
  scopeSearch: string;
}) {
  const [validation, setValidation] = useState<UploadValidationResult | null>(null);
  const [session, setSession] = useState<UploadSession | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isBusy, setIsBusy] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const queryClient = useQueryClient();
  const { queryKey } = useScope();
  const runQueryKey = [...queryKey('ingestion-run'), session?.runId];
  const hasRunSession = Boolean(
    session?.runId &&
      session.submitted &&
      !session.cancelled,
  );
  const cachedRun = queryClient.getQueryData<BatchStatusResponse>(runQueryKey);
  const runQuery = useQuery({
    queryKey: runQueryKey,
    queryFn: ({ signal }) => loadBatchStatus(client, session!.runId!, signal),
    enabled: hasRunSession && !cachedRun?.terminal,
    refetchInterval: (query) =>
      query.state.error || query.state.data?.terminal ? false : POLL_INTERVAL_MS,
    refetchOnMount: (query) => !query.state.data?.terminal,
    retry: false,
  });

  useEffect(() => () => controllerRef.current?.abort(), []);

  function selectFiles(files: readonly File[]) {
    const result = validateUploadFiles(files);
    setValidation(result);
    if (!result.valid) {
      setSession(null);
      return;
    }

    setSession({
      idempotencyKey: createIdempotencyKey(),
      rows: createUploadRows(files),
      submitted: false,
      replay: false,
      cancelled: false,
    });
  }

  function handleInputChange(event: React.ChangeEvent<HTMLInputElement>) {
    selectFiles(Array.from(event.currentTarget.files ?? []));
    event.currentTarget.value = '';
  }

  function handleDrop(event: React.DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragging(false);
    if (!disabled && !busy) selectFiles(Array.from(event.dataTransfer.files));
  }

  function handleDragOver(event: React.DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (!disabled && !busy) setIsDragging(true);
  }

  function handleDragLeave(event: React.DragEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) setIsDragging(false);
  }

  function submit() {
    if (!session || busy || disabled) return;
    void runUpload(session);
  }

  function retry() {
    if (!session || busy || disabled || (!retryable && !retryRequest)) return;
    void runUpload({ ...session, rows: effectiveRows });
  }

  function cancel() {
    if (session?.runId) void queryClient.cancelQueries({ queryKey: runQueryKey });
    controllerRef.current?.abort();
    setSession((current) =>
      current
        ? {
            ...current,
            cancelled: true,
            rows: effectiveRows.map((row) => (isActiveRow(row) ? { ...row, state: 'cancelled' } : row)),
          }
        : current,
    );
  }

  function resumeObservation() {
    if (!session?.runId || busy) return;
    void queryClient.invalidateQueries({ queryKey: runQueryKey });
    setSession((current) => (current ? { ...current, cancelled: false, error: undefined } : current));
  }

  async function runUpload(current: UploadSession) {
    const controller = new AbortController();
    controllerRef.current = controller;
    setIsBusy(true);
    setSession((value) =>
      value && value.idempotencyKey === current.idempotencyKey
        ? {
            ...value,
            submitted: true,
            cancelled: false,
            error: undefined,
            rows: value.rows.map((row) =>
              !value.submitted || row.retryable || value.error ? { ...row, state: 'uploading' } : row,
            ),
          }
        : value,
    );

    try {
      const response = await submitDocumentBatch(client, current.rows, current.idempotencyKey, controller.signal);
      if (controller.signal.aborted) return;
      const statusResponse: BatchStatusResponse = {
        items: response.items,
        run_id: response.run_id,
        run_state: response.run_state,
        schema_version: response.schema_version,
        status: response.status,
        terminal: response.terminal,
      };
      queryClient.setQueryData(
        [...queryKey('ingestion-run'), response.run_id],
        statusResponse,
      );
      setSession((value) =>
        value && value.idempotencyKey === current.idempotencyKey
          ? {
              ...value,
              replay: response.replay,
              runId: response.run_id,
              cancelled: false,
            }
          : value,
      );
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) {
        setSession((value) =>
          value && value.idempotencyKey === current.idempotencyKey
            ? {
                ...value,
                cancelled: true,
                rows: effectiveRows.map((row) => (isActiveRow(row) ? { ...row, state: 'cancelled' } : row)),
              }
            : value,
        );
      } else {
        setSession((value) =>
          value && value.idempotencyKey === current.idempotencyKey
            ? {
                ...value,
                error,
                rows: value.rows.map((row) => (isActiveRow(row) ? { ...row, state: 'failed' } : row)),
              }
            : value,
        );
      }
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
      setIsBusy(false);
    }
  }

  const effectiveRows = session?.cancelled
    ? session.rows
    : session && runQuery.data
      ? applyBatchItems(session.rows, runQuery.data.items)
      : session?.rows ?? [];
  const effectiveStatus = runQuery.data?.status;
  const requestError = session?.error ?? runQuery.error;
  const retryable = effectiveRows.some((row) => row.retryable);
  const retryRequest = isRetryableRequestError(session?.error);
  const isRunQueryActive = Boolean(hasRunSession && !runQuery.data?.terminal && !runQuery.error);
  const isObserving = Boolean(
      hasRunSession &&
      !runQuery.data?.terminal &&
      !runQuery.error &&
      runQuery.data,
  );
  const observationError = Boolean(runQuery.error);
  const busy = isBusy || isObserving || (isRunQueryActive && runQuery.isPending);
  const canSubmit = Boolean(session && !session.submitted && !disabled);

  return (
    <section className={styles.uploadPanel} aria-labelledby="document-upload-heading">
      <div className={styles.uploadHeader}>
        <div>
          <h3 id="document-upload-heading">{ru.documents.upload.title}</h3>
          <p>{ru.documents.upload.description}</p>
        </div>
        <span className={styles.uploadLimit}>{ru.documents.upload.acceptedTypes}</span>
      </div>

      <div
        aria-label={ru.documents.upload.dropzone}
        className={`${styles.dropzone} ${isDragging ? styles.dropzoneActive : ''}`}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        <strong>{ru.documents.upload.dropzone}</strong>
        <span>{ru.documents.upload.or}</span>
        <label className={styles.primaryButton} htmlFor="document-upload-input">
          {ru.documents.upload.chooseFiles}
        </label>
        <input
          accept={DOCUMENT_UPLOAD_ACCEPT}
          className={styles.visuallyHidden}
          disabled={disabled || busy}
          id="document-upload-input"
          multiple
          onChange={handleInputChange}
          type="file"
        />
      </div>

      {validation && !validation.valid ? <ValidationErrors result={validation} /> : null}

      {session ? (
        <div className={styles.uploadQueue} aria-label={ru.documents.upload.review} role="region">
          <div className={styles.uploadQueueHeader}>
            <div>
              <h4>{ru.documents.upload.review}</h4>
              <p>{ru.documents.upload.selectedCount.replace('{count}', String(session.rows.length))}</p>
            </div>
            {session.replay ? <span className={styles.replayNotice}>{ru.documents.upload.replay}</span> : null}
          </div>

          <ul className={styles.uploadList}>
            {effectiveRows.map((row) => (
              <UploadRowView key={row.id} row={row} />
            ))}
          </ul>

          <div className={styles.uploadActions}>
            {canSubmit ? (
              <button className={styles.primaryButton} disabled={isBusy} onClick={submit} type="button">
                {ru.documents.upload.submit}
              </button>
            ) : null}
            {busy ? (
              <button className={styles.secondaryButton} onClick={cancel} type="button">
                {ru.documents.upload.cancel}
              </button>
            ) : null}
            {(retryable || retryRequest) && !busy ? (
              <button className={styles.secondaryButton} onClick={retry} type="button">
                {retryRequest ? ru.documents.upload.retryRequest : ru.documents.upload.retry}
              </button>
            ) : null}
            {(session.cancelled || observationError) && session.runId && !busy ? (
              <button className={styles.secondaryButton} onClick={resumeObservation} type="button">
                {ru.documents.upload.resumeObservation}
              </button>
            ) : null}
            {session.runId ? (
              <Link
                className={styles.secondaryButton}
                to={{ pathname: `/runs/${session.runId}`, search: `?${scopeSearch}` }}
              >
                {ru.documents.upload.openRun}
              </Link>
            ) : null}
          </div>

          {effectiveStatus ? <p className={styles.uploadBatchStatus}>{batchStatusLabel(effectiveStatus)}</p> : null}
          {session.cancelled ? <p className={styles.uploadCancellation}>{ru.documents.upload.cancelledDescription}</p> : null}
          {requestError ? (
            <>
              <ApiErrorPanel error={requestError} />
              {!retryRequest ? (
                <p className={styles.uploadBatchStatus}>
                  {observationError ? ru.documents.upload.observationNextAction : ru.documents.upload.errorNextAction}
                </p>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function UploadRowView({ row }: { row: UploadRow }) {
  const label = uploadStateLabel(row);
  const description = uploadStateDescription(row);
  const warnings = row.serverItem?.warnings.length ?? 0;
  const stateClass = styles[`uploadState_${row.state}`] ?? '';
  const progress = uploadProgress(row.state);

  return (
    <li className={styles.uploadRow}>
      <div className={styles.uploadFileIdentity}>
        <strong>{row.file.name}</strong>
        <span>{formatBytes(row.file.size)}</span>
      </div>
      <div className={styles.uploadFileOutcome}>
        <span className={`${styles.uploadState} ${stateClass}`}>{label}</span>
        {progress ? <progress aria-label={`${row.file.name}: ${label}`} className={styles.uploadProgress} max={100} /> : null}
        <span>{description}</span>
        {warnings > 0 ? <span>{ru.documents.upload.warning}</span> : null}
        {row.retryable ? <span>{ru.documents.upload.retryable}</span> : null}
      </div>
    </li>
  );
}

function ValidationErrors({ result }: { result: UploadValidationResult }) {
  return (
    <div className={styles.uploadValidation} role="alert">
      <strong>{ru.documents.upload.review}</strong>
      <ul>
        {result.issues.map((issue, index) => (
          <li key={`${issue.code}-${index}`}>{validationMessage(issue)}</li>
        ))}
      </ul>
    </div>
  );
}

function validationMessage(issue: UploadValidationIssue): string {
  return ru.documents.upload.validation[issue.code];
}

function uploadStateLabel(row: UploadRow): string {
  if (row.state === 'cancelled' && (row.serverItem?.status === 'cancelled' || row.serverItem?.processing === 'cancelled')) {
    return ru.documents.upload.serverCancelled;
  }
  if (row.state === 'processing' && row.serverItem?.item_state === 'projection_requested') {
    return ru.documents.states.indexing;
  }
  if (row.state === 'processing' && row.serverItem?.item_state === 'processing') {
    return ru.documents.states.parsing;
  }
  if (row.state === 'failed' && (row.serverItem?.status === 'partial' || row.serverItem?.processing === 'partial')) {
    return ru.documents.upload.partial;
  }
  return ru.documents.upload[row.state];
}

function uploadStateDescription(row: UploadRow): string {
  if (row.state === 'accepted') return ru.documents.upload.acceptedDescription;
  if (row.state === 'processing' && row.serverItem?.item_state === 'projection_requested') {
    return ru.documents.upload.indexingDescription;
  }
  if (row.state === 'processing') return ru.documents.upload.processingDescription;
  if (row.state === 'ready') return ru.documents.upload.readyDescription;
  if (row.state === 'failed' && (row.serverItem?.status === 'partial' || row.serverItem?.processing === 'partial')) {
    return ru.documents.upload.partialDescription;
  }
  if (row.state === 'failed' || row.state === 'rejected') return ru.documents.upload.serverFailureDescription;
  if (row.state === 'cancelled') {
    return row.serverItem?.status === 'cancelled' || row.serverItem?.processing === 'cancelled'
      ? ru.documents.upload.serverCancelledDescription
      : ru.documents.upload.cancelledDescription;
  }
  return '';
}

function uploadProgress(state: UploadRow['state']): boolean {
  return state === 'uploading' || state === 'accepted' || state === 'processing';
}

function batchStatusLabel(status: components['schemas']['PublicBatchStatus']): string {
  if (status === 'processing') return ru.documents.upload.processing;
  if (status === 'succeeded') return ru.documents.upload.ready;
  if (status === 'partial') return ru.documents.upload.partial;
  if (status === 'failed') return ru.documents.upload.failed;
  return ru.documents.upload.cancelled;
}

function isActiveRow(row: UploadRow): boolean {
  return row.state === 'queued' || row.state === 'uploading' || row.state === 'accepted' || row.state === 'processing';
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}

function isRetryableRequestError(error: unknown): boolean {
  return (
    error instanceof SpineApiError &&
    (error.retryability === 'same_command' || error.retryability === 'after_delay')
  );
}
