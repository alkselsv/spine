import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';

import type { components } from '../../../api/generated/schema';
import { SpineApiError } from '../../api/client';
import { ApiErrorPanel } from '../../app/status';
import { ru } from '../../i18n/resources';
import {
  DEFAULT_PROCESSING_PROFILE,
  loadProcessingCommand,
  reprocessDocument,
  retryDocumentProcessing,
  type ProcessingCommand,
} from './processing-api';
import styles from './ProcessingCommandAction.module.css';

type ProcessingCommandActionProps = {
  client: Parameters<typeof loadProcessingCommand>[0];
  queryKey: readonly unknown[];
  sourceObjectId: string;
  sourceRevisionId: string;
  currentRevisionId: string;
  processingReceiptId?: string | null;
  mode:
    | { kind: 'retry'; failureId: string; retryable: boolean }
    | { kind: 'reprocess' };
};

const ACTIVE_STATUSES: readonly components['schemas']['ProcessingCommandStatus'][] = [
  'accepted',
  'processing',
];

export function ProcessingCommandAction(props: ProcessingCommandActionProps) {
  const queryClient = useQueryClient();
  const storageKey = idempotencyStorageKey(props);
  const idempotencyKey = useRef<string | null>(readIdempotencyKey(storageKey));
  const [receipt, setReceipt] = useState<ProcessingCommand | null>(null);
  const commandMutation = useMutation({
    mutationFn: (key: string) => {
      const signal = new AbortController().signal;
      if (props.mode.kind === 'retry') {
        return retryDocumentProcessing(
          props.client,
          {
            expected_current_revision_id: props.currentRevisionId,
            parent_failure_id: props.mode.failureId,
            parent_processing_receipt_id: props.processingReceiptId ?? null,
            source_object_id: props.sourceObjectId,
            source_revision_id: props.sourceRevisionId,
          },
          key,
          signal,
        );
      }
      return reprocessDocument(
        props.client,
        {
          expected_current_revision_id: props.currentRevisionId,
          parent_processing_receipt_id: props.processingReceiptId ?? null,
          processing_profile_reference: DEFAULT_PROCESSING_PROFILE,
          source_object_id: props.sourceObjectId,
          source_revision_id: props.sourceRevisionId,
        },
        key,
        signal,
      );
    },
    onSuccess: (command, key) => {
      idempotencyKey.current = null;
      clearIdempotencyKey(storageKey, key);
      setReceipt(command);
      void queryClient.invalidateQueries({ queryKey: props.queryKey });
      void queryClient.invalidateQueries({ queryKey: ['spine'] });
    },
  });

  const statusQuery = useQuery({
    queryKey: [...props.queryKey, 'processing-command', props.sourceObjectId, receipt?.operation_id],
    queryFn: ({ signal }) => loadProcessingCommand(props.client, receipt!.operation_id, signal),
    enabled: Boolean(receipt?.operation_id && ACTIVE_STATUSES.includes(receipt.status)),
    initialData: receipt ?? undefined,
    refetchInterval: (query) =>
      query.state.data && ACTIVE_STATUSES.includes(query.state.data.status) ? 750 : false,
    refetchOnMount: true,
  });

  if (props.mode.kind === 'retry' && !props.mode.retryable) return null;

  const command = statusQuery.data ?? receipt;
  const actionLabel = props.mode.kind === 'retry' ? ru.documents.commands.retry : ru.documents.commands.reprocess;
  const isConflict = commandMutation.error instanceof SpineApiError && commandMutation.error.status === 409;
  const canSubmit =
    !commandMutation.error &&
    !commandMutation.isPending &&
    (!command || !ACTIVE_STATUSES.includes(command.status));

  function submit() {
    idempotencyKey.current ??= crypto.randomUUID();
    storeIdempotencyKey(storageKey, idempotencyKey.current);
    commandMutation.mutate(idempotencyKey.current);
  }

  return (
    <div className={styles.panel}>
      {commandMutation.isPending ? (
        <p role="status">
          {props.mode.kind === 'retry' ? ru.documents.commands.retrying : ru.documents.commands.submitting}
        </p>
      ) : null}
      {canSubmit ? (
        <button
          className={styles.action}
          onClick={submit}
          type="button"
        >
          {actionLabel}
        </button>
      ) : null}
      {commandMutation.error ? (
        <div className={styles.feedback}>
          <ApiErrorPanel error={commandMutation.error} />
          {isConflict ? (
            <>
              <p>{ru.documents.commands.conflictNextAction}</p>
              <button
                className={styles.action}
                disabled={commandMutation.isPending}
                onClick={() => {
                  idempotencyKey.current = null;
                  clearIdempotencyKey(storageKey);
                  void queryClient.invalidateQueries({ queryKey: ['spine'] }).then(() => commandMutation.reset());
                }}
                type="button"
              >
                {ru.documents.commands.refreshRevision}
              </button>
            </>
          ) : commandMutation.error instanceof SpineApiError && commandMutation.error.retryability !== 'never' ? (
            <button className={styles.action} disabled={commandMutation.isPending} onClick={submit} type="button">
              {ru.documents.commands.retrySameCommand}
            </button>
          ) : null}
        </div>
      ) : null}
      {statusQuery.error ? (
        <div className={styles.feedback}>
          <ApiErrorPanel error={statusQuery.error} />
          <button className={styles.action} onClick={() => void statusQuery.refetch()} type="button">
            {ru.documents.commands.refreshStatus}
          </button>
        </div>
      ) : null}
      {command ? <ProcessingCommandReceipt command={command} /> : null}
    </div>
  );
}

function idempotencyStorageKey(props: ProcessingCommandActionProps): string {
  const failureId = props.mode.kind === 'retry' ? props.mode.failureId : null;
  return `spine:document-processing-command:${JSON.stringify([
    props.queryKey,
    props.mode.kind,
    props.sourceObjectId,
    props.sourceRevisionId,
    props.currentRevisionId,
    failureId,
  ])}`;
}

function readIdempotencyKey(storageKey: string): string | null {
  try {
    return window.sessionStorage.getItem(storageKey);
  } catch {
    return null;
  }
}

function storeIdempotencyKey(storageKey: string, value: string): void {
  try {
    window.sessionStorage.setItem(storageKey, value);
  } catch {
    // The in-memory key still keeps retries stable when browser storage is unavailable.
  }
}

function clearIdempotencyKey(storageKey: string, expectedValue?: string): void {
  try {
    if (expectedValue && window.sessionStorage.getItem(storageKey) !== expectedValue) return;
    window.sessionStorage.removeItem(storageKey);
  } catch {
    // The operation remains safe through the server's idempotency contract.
  }
}

function ProcessingCommandReceipt({ command }: { command: ProcessingCommand }) {
  const [searchParams] = useSearchParams();
  return (
    <section className={styles.receipt} aria-label={ru.documents.commands.receiptTitle} aria-live="polite">
      <strong>{ru.documents.commands.status[command.status]}</strong>
      <dl>
        <div><dt>{ru.documents.commands.operationId}</dt><dd><code>{command.operation_id}</code></dd></div>
        <div><dt>{ru.documents.commands.runId}</dt><dd><code>{command.run_id}</code></dd></div>
        <div><dt>{ru.documents.commands.itemId}</dt><dd><code>{command.item_id}</code></dd></div>
        {command.processing_receipt_id ? (
          <div><dt>{ru.documents.commands.processingReceipt}</dt><dd><code>{command.processing_receipt_id}</code></dd></div>
        ) : null}
      </dl>
      {command.failure_ids.length ? (
        <p>{ru.documents.commands.failures}: {command.failure_ids.map((id) => <code key={id}>{id} </code>)}</p>
      ) : null}
      {command.replay ? <p>{ru.documents.commands.replayed}</p> : null}
      {command.status === 'accepted' || command.status === 'processing' ? (
        <p>{command.status === 'accepted' ? ru.documents.commands.queued : ru.documents.commands.polling}</p>
      ) : null}
      <Link to={{ pathname: `/runs/${command.run_id}`, search: `?${searchParams.toString()}` }}>
        {ru.documents.processingRun}
      </Link>
    </section>
  );
}
