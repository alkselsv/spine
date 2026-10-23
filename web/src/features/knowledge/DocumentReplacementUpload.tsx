import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { SpineApiError, type ApiScope, type ScopedApiClient } from '../../api/client';
import { ApiErrorPanel } from '../../app/status';
import { ru } from '../../i18n/resources';
import { createIdempotencyKey } from './upload';
import {
  loadReplacementUploadStatus,
  submitDocumentRevisionUpload,
  type ReplacementUploadResponse,
} from './replacement-upload-api';
import styles from './DocumentDetailPage.module.css';

interface ReplacementSession {
  file: File;
  expectedRevisionId: string;
  idempotencyKey: string;
  upload: ReplacementUploadResponse | null;
  error: unknown | null;
}

export function DocumentReplacementUpload({
  client,
  currentRevisionId,
  onRefresh,
  queryKey,
  scope,
  sourceObjectId,
}: {
  client: ScopedApiClient;
  currentRevisionId: string | null | undefined;
  onRefresh: () => void | Promise<unknown>;
  queryKey: (resource: string) => readonly unknown[];
  scope: ApiScope;
  sourceObjectId: string;
}) {
  const [session, setSession] = useState<ReplacementSession | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const queryClient = useQueryClient();
  const completedRun = useRef<string | null>(null);
  const upload = session?.upload;
  const statusQuery = useQuery({
    queryKey: [
      'spine', 'replacement-upload-status', scope.workspaceId, scope.environmentId,
      sourceObjectId, upload?.run_id, upload?.item_id,
    ],
    queryFn: ({ signal }) => loadReplacementUploadStatus(client, upload!.run_id, upload!.item_id, signal),
    enabled: Boolean(upload && !upload.terminal),
    refetchInterval: (query) => query.state.data?.terminal ? false : 750,
    retry: false,
  });

  useEffect(() => {
    const status = statusQuery.data;
    if (!status?.terminal || status.acceptance !== 'accepted' || completedRun.current === status.run_id) return;
    completedRun.current = status.run_id;
    void queryClient.invalidateQueries({ queryKey: [...queryKey('document-detail'), sourceObjectId] });
    void queryClient.invalidateQueries({
      queryKey: ['spine', 'document-revisions', scope.workspaceId, scope.environmentId, sourceObjectId],
    });
    void queryClient.invalidateQueries({ queryKey: queryKey('projection-status') });
    void onRefresh();
  }, [onRefresh, queryClient, queryKey, scope.environmentId, scope.workspaceId, sourceObjectId, statusQuery.data]);

  function selectFile(file: File | undefined) {
    completedRun.current = null;
    setSession(file && currentRevisionId
      ? { file, expectedRevisionId: currentRevisionId, idempotencyKey: createIdempotencyKey(), upload: null, error: null }
      : null);
  }

  async function submit(retry = false) {
    if (!session || !currentRevisionId || submitting) return;
    const active = retry ? session : { ...session, error: null };
    setSubmitting(true);
    setSession(active);
    try {
      const response = await submitDocumentRevisionUpload(
        client, sourceObjectId, active.expectedRevisionId, active.file, active.idempotencyKey, new AbortController().signal,
      );
      setSession({ ...active, upload: response, error: null });
      if (response.terminal && response.acceptance === 'accepted') {
        completedRun.current = response.run_id;
        void queryClient.invalidateQueries({ queryKey: [...queryKey('document-detail'), sourceObjectId] });
        void queryClient.invalidateQueries({
          queryKey: ['spine', 'document-revisions', scope.workspaceId, scope.environmentId, sourceObjectId],
        });
        void onRefresh();
      }
    } catch (error) {
      setSession({ ...active, error });
    } finally {
      setSubmitting(false);
    }
  }

  const status = statusQuery.data;
  const retryable = session?.error instanceof SpineApiError &&
    session.error.retryability !== 'never' && session.error.status !== 409;
  const phase = status?.status ?? upload?.status ?? null;

  return (
    <div className={styles.lifecycleForm}>
      <h4>{ru.documents.documentActions.replacementTitle}</h4>
      <p className={styles.cardDescription}>{ru.documents.documentActions.replacementDescription}</p>
      <label className={styles.lifecycleField}>
        {ru.documents.documentActions.replacementFile}
        <input
          accept=".pdf,.docx,.xlsx,.md,.txt,text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          disabled={submitting || Boolean(upload && !upload.terminal)}
          onChange={(event) => selectFile(event.currentTarget.files?.[0])}
          type="file"
        />
      </label>
      {session ? <p>{session.file.name}</p> : null}
      <button
        className={styles.lifecyclePrimaryButton}
        disabled={!session || !currentRevisionId || submitting || Boolean(upload && !upload.terminal) || Boolean(session.error && !retryable)}
        onClick={() => void submit(Boolean(session?.error))}
        type="button"
      >
        {submitting ? ru.documents.documentActions.replacementUploading : session?.error ? ru.documents.documentActions.retry : ru.documents.documentActions.submitReplacement}
      </button>
      {phase ? <p role="status">{replacementStatusLabel(phase)}</p> : null}
      {status?.source_revision_id || upload?.source_revision_id ? (
        <p>{ru.documents.documentActions.acceptedRevision}: <code>{status?.source_revision_id ?? upload?.source_revision_id}</code></p>
      ) : null}
      {session?.error ? <ApiErrorPanel error={session.error} /> : null}
      {statusQuery.error ? <ApiErrorPanel error={statusQuery.error} /> : null}
    </div>
  );
}

function replacementStatusLabel(status: string): string {
  if (status === 'non_terminal' || status === 'processing') return ru.documents.documentActions.replacementRunning;
  if (status === 'succeeded') return ru.documents.documentActions.replacementSucceeded;
  if (status === 'partial') return ru.documents.documentActions.replacementPartial;
  if (status === 'rejected' || status === 'quarantined' || status === 'unsupported') return ru.documents.documentActions.replacementRejected;
  return ru.documents.documentActions.replacementFailed;
}
