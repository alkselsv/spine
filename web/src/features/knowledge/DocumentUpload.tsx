import { useRef, useState } from 'react';
import { Link } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { SpineApiError, type ScopedApiClient } from '../../api/client';
import { ApiErrorPanel } from '../../app/status';
import { ru } from '../../i18n/resources';
import {
  applyBatchItems,
  createIdempotencyKey,
  createUploadRows,
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
  status?: components['schemas']['PublicBatchStatus'];
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
    if (!disabled && !isBusy) selectFiles(Array.from(event.dataTransfer.files));
  }

  function handleDragOver(event: React.DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (!disabled && !isBusy) setIsDragging(true);
  }

  function handleDragLeave(event: React.DragEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) setIsDragging(false);
  }

  function submit() {
    if (!session || isBusy || disabled) return;
    void runUpload(session);
  }

  function retry() {
    if (!session || isBusy || disabled || (!session.rows.some((row) => row.retryable) && !isRetryableRequestError(session.error))) return;
    void runUpload(session);
  }

  function cancel() {
    controllerRef.current?.abort();
    setSession((current) =>
      current
        ? {
            ...current,
            cancelled: true,
            rows: current.rows.map((row) => (isActiveRow(row) ? { ...row, state: 'cancelled' } : row)),
          }
        : current,
    );
  }

  function resumeObservation() {
    if (!session?.runId || isBusy) return;
    void observe(session, session.runId);
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
      setSession((value) =>
        value && value.idempotencyKey === current.idempotencyKey
          ? {
              ...value,
              rows: applyBatchItems(value.rows, response.items),
              replay: response.replay,
              runId: response.run_id,
              status: response.status,
              cancelled: false,
            }
          : value,
      );
      if (!response.terminal) await observe(current, response.run_id, controller);
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) {
        setSession((value) =>
          value && value.idempotencyKey === current.idempotencyKey
            ? {
                ...value,
                cancelled: true,
                rows: value.rows.map((row) => (isActiveRow(row) ? { ...row, state: 'cancelled' } : row)),
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

  async function observe(current: UploadSession, runId: string, existingController?: AbortController) {
    const controller = existingController ?? new AbortController();
    if (!existingController) controllerRef.current = controller;
    setIsBusy(true);
    setSession((value) =>
      value && value.idempotencyKey === current.idempotencyKey
        ? { ...value, cancelled: false, error: undefined, runId }
        : value,
    );

    try {
      let response = await loadBatchStatus(client, runId, controller.signal);
      while (true) {
        if (controller.signal.aborted) return;
        applyStatus(current.idempotencyKey, response);
        if (response.terminal) return;
        await waitForPoll(controller.signal);
        response = await loadBatchStatus(client, runId, controller.signal);
      }
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) {
        setSession((value) =>
          value && value.idempotencyKey === current.idempotencyKey
            ? {
                ...value,
                cancelled: true,
                rows: value.rows.map((row) => (isActiveRow(row) ? { ...row, state: 'cancelled' } : row)),
              }
            : value,
        );
      } else {
        setSession((value) =>
          value && value.idempotencyKey === current.idempotencyKey ? { ...value, error } : value,
        );
      }
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
      setIsBusy(false);
    }
  }

  function applyStatus(idempotencyKey: string, response: BatchStatusResponse) {
    setSession((value) =>
      value && value.idempotencyKey === idempotencyKey
        ? {
            ...value,
            rows: applyBatchItems(value.rows, response.items),
            runId: response.run_id,
            status: response.status,
            cancelled: false,
          }
        : value,
    );
  }

  const retryable = session?.rows.some((row) => row.retryable) ?? false;
  const retryRequest = isRetryableRequestError(session?.error);
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
        <span>или</span>
        <label className={styles.primaryButton} htmlFor="document-upload-input">
          {ru.documents.upload.chooseFiles}
        </label>
        <input
          accept=".txt,.md,.markdown,text/plain,text/markdown"
          className={styles.visuallyHidden}
          disabled={disabled || isBusy}
          id="document-upload-input"
          multiple
          onChange={handleInputChange}
          type="file"
        />
      </div>

      {validation && !validation.valid ? <ValidationErrors result={validation} /> : null}

      {session ? (
        <div className={styles.uploadQueue} aria-label={ru.documents.upload.review}>
          <div className={styles.uploadQueueHeader}>
            <div>
              <h4>{ru.documents.upload.review}</h4>
              <p>{ru.documents.upload.selectedCount.replace('{count}', String(session.rows.length))}</p>
            </div>
            {session.replay ? <span className={styles.replayNotice}>{ru.documents.upload.replay}</span> : null}
          </div>

          <ul className={styles.uploadList}>
            {session.rows.map((row) => (
              <UploadRowView key={row.id} row={row} />
            ))}
          </ul>

          <div className={styles.uploadActions}>
            {canSubmit ? (
              <button className={styles.primaryButton} disabled={isBusy} onClick={submit} type="button">
                {ru.documents.upload.submit}
              </button>
            ) : null}
            {isBusy ? (
              <button className={styles.secondaryButton} onClick={cancel} type="button">
                {ru.documents.upload.cancel}
              </button>
            ) : null}
            {(retryable || retryRequest) && !isBusy ? (
              <button className={styles.secondaryButton} onClick={retry} type="button">
                {retryRequest ? ru.documents.upload.retryRequest : ru.documents.upload.retry}
              </button>
            ) : null}
            {session.cancelled && session.runId && !isBusy ? (
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

          {session.status ? <p className={styles.uploadBatchStatus}>{batchStatusLabel(session.status)}</p> : null}
          {session.cancelled ? <p className={styles.uploadCancellation}>{ru.documents.upload.cancelledDescription}</p> : null}
          {session.error ? (
            <>
              <ApiErrorPanel error={session.error} />
              {!retryRequest ? <p className={styles.uploadBatchStatus}>{ru.documents.upload.errorNextAction}</p> : null}
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

  return (
    <li className={styles.uploadRow}>
      <div className={styles.uploadFileIdentity}>
        <strong>{row.file.name}</strong>
        <span>{formatBytes(row.file.size)}</span>
      </div>
      <div className={styles.uploadFileOutcome}>
        <span className={`${styles.uploadState} ${styles[`uploadState_${row.state}`]}`}>{label}</span>
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
          <li key={`${issue.code}-${issue.filename ?? 'batch'}-${index}`}>{validationMessage(issue)}</li>
        ))}
      </ul>
    </div>
  );
}

function validationMessage(issue: UploadValidationIssue): string {
  const message = ru.documents.upload.validation[issue.code];
  return message.replace('{filename}', issue.filename ?? '');
}

function uploadStateLabel(row: UploadRow): string {
  if (row.state === 'processing' && row.serverItem?.item_state === 'projection_requested') {
    return ru.documents.states.indexing;
  }
  if (row.state === 'processing' && row.serverItem?.item_state === 'processing') {
    return ru.documents.states.parsing;
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
  if (row.state === 'cancelled') return ru.documents.upload.cancelledDescription;
  return '';
}

function batchStatusLabel(status: components['schemas']['PublicBatchStatus']): string {
  if (status === 'processing') return ru.documents.upload.processing;
  if (status === 'succeeded') return ru.documents.upload.ready;
  if (status === 'partial') return ru.documents.upload.failed;
  if (status === 'failed') return ru.documents.upload.failed;
  return ru.documents.upload.cancelled;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} Б`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} КБ`;
  return `${(value / (1024 * 1024)).toFixed(1)} МБ`;
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

function waitForPoll(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    let timeout = 0;
    const abort = () => {
      window.clearTimeout(timeout);
      signal.removeEventListener('abort', abort);
      const error = new Error('The operation was aborted.');
      error.name = 'AbortError';
      reject(error);
    };
    const finish = () => {
      signal.removeEventListener('abort', abort);
      resolve();
    };
    timeout = window.setTimeout(finish, POLL_INTERVAL_MS);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}
