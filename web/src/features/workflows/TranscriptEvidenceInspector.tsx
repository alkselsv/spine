import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { apiQueryOptions } from '../../api/query';
import type { TranscriptTimeRangeLocator } from './transcript-inspection';
import { inspectTranscriptEvidence } from './transcript-inspection';
import { reviewTranscript, type TranscriptReviewDecision, type TranscriptReviewResult } from './transcript-review';

export function TranscriptEvidenceInspector({
  client,
  locator,
  scope,
  onNavigate,
}: {
  client: Pick<ScopedApiClient, 'POST'>;
  locator: TranscriptTimeRangeLocator;
  scope: Scope;
  onNavigate?: (startMs: number, endMs: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [reviewResult, setReviewResult] = useState<TranscriptReviewResult | null>(null);
  const query = useQuery({
    ...apiQueryOptions({
      load: (signal) => inspectTranscriptEvidence(client, scope, locator, signal),
      queryKey: [
        'transcript-evidence', scope.workspaceId, scope.environmentId,
        locator.transcript_artifact_id, locator.transcript_artifact_digest,
        locator.start_ms, locator.end_ms, ...locator.segment_ids,
      ],
    }),
    enabled: open,
    staleTime: 0,
  });

  const result = query.error ? undefined : query.data;
  return (
    <section aria-label="Проверка фрагмента расшифровки">
      <button type="button" onClick={() => setOpen((value) => !value)}>
        {open ? 'Скрыть фрагмент' : 'Открыть фрагмент расшифровки'}
      </button>
      {open && query.isPending ? <p role="status">Загрузка расшифровки…</p> : null}
      {open && query.error ? <p role="alert">Не удалось проверить доступ к фрагменту расшифровки.</p> : null}
      {open && result?.status === 'current' ? (
        <div>
          <p role="status">{result.text}</p>
          <dl>
            <dt>Время</dt>
            <dd>{formatTime(locator.start_ms)}–{formatTime(locator.end_ms)}</dd>
            <dt>Ревизия записи</dt>
            <dd>{result.source_revision_id}</dd>
            <dt>Версия расшифровки</dt>
            <dd>{result.transcript_artifact_id} · {result.transcript_artifact_schema_version}</dd>
            <dt>Публикация</dt>
            <dd>{result.publication_state}</dd>
            <dt>Проверка человеком</dt>
            <dd>{result.review_state === 'approved' ? 'Одобрена' : 'Не требуется политикой'}</dd>
            {result.partial ? <><dt>Полнота</dt><dd>Частичная расшифровка</dd></> : null}
          </dl>
          {onNavigate ? (
            <button type="button" onClick={() => onNavigate(locator.start_ms, locator.end_ms)}>
              Перейти к фрагменту записи
            </button>
          ) : null}
          <button type="button" onClick={() => void query.refetch()}>Проверить доступ снова</button>
          {reviewResult ? (
            <section aria-label="Результат проверки расшифровки">
              <strong>{reviewStatusLabel(reviewResult.review.decision)}</strong>
              {reviewResult.corrected_artifact ? <p>Создана исправленная версия расшифровки.</p> : null}
              <p>{reviewResult.review.publication_eligible ? 'Публикация разрешена политикой.' : 'Публикация запрещена политикой.'}</p>
            </section>
          ) : result.review_state !== 'approved' ? (
            <TranscriptReviewControls
              client={client}
              scope={scope}
              artifactId={locator.transcript_artifact_id}
              artifactDigest={locator.transcript_artifact_digest}
              startMs={locator.start_ms}
              endMs={locator.end_ms}
              text={result.text ?? ''}
              onReviewed={setReviewResult}
            />
          ) : null}
        </div>
      ) : null}
      {open && result && result.status !== 'current' ? (
        <p role="status">{statusLabel(result.status)}</p>
      ) : null}
    </section>
  );
}

function TranscriptReviewControls({
  client,
  scope,
  artifactId,
  artifactDigest,
  startMs,
  endMs,
  text,
  onReviewed,
}: {
  client: Pick<ScopedApiClient, 'POST'>;
  scope: Scope;
  artifactId: string;
  artifactDigest: string;
  startMs: number;
  endMs: number;
  text: string;
  onReviewed: (result: TranscriptReviewResult) => void;
}) {
  const [rationale, setRationale] = useState('Проверено по исходной записи.');
  const [correction, setCorrection] = useState(text);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState(false);
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());

  async function submit(decision: TranscriptReviewDecision) {
    setPending(true);
    setFailure(false);
    try {
      const result = await reviewTranscript(client, scope, {
        artifactId,
        artifactDigest,
        decision,
        rationale,
        idempotencyKey,
        correctedSegment: decision === 'correct' ? { startMs, endMs, text: correction } : undefined,
      });
      onReviewed(result);
      setIdempotencyKey(crypto.randomUUID());
    } catch {
      setFailure(true);
    } finally {
      setPending(false);
    }
  }

  return (
    <form aria-label="Решение по расшифровке" onSubmit={(event) => event.preventDefault()}>
      <h3>Проверка версии ASR</h3>
      <label>
        Исправление фрагмента
        <textarea value={correction} onChange={(event) => setCorrection(event.target.value)} />
      </label>
      <label>
        Обоснование решения
        <textarea value={rationale} onChange={(event) => setRationale(event.target.value)} />
      </label>
      {failure ? <p role="alert">Не удалось сохранить решение. Проверьте доступ и версию расшифровки.</p> : null}
      <button type="button" disabled={pending || !rationale.trim()} onClick={() => void submit('approve')}>
        Одобрить
      </button>
      <button type="button" disabled={pending || !rationale.trim() || !correction.trim()} onClick={() => void submit('correct')}>
        Исправить и сохранить
      </button>
      <button type="button" disabled={pending || !rationale.trim()} onClick={() => void submit('reject')}>
        Отклонить
      </button>
    </form>
  );
}

function formatTime(milliseconds: number): string {
  const totalSeconds = Math.floor(milliseconds / 1_000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const millis = milliseconds % 1_000;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`;
}

function statusLabel(status: string): string {
  switch (status) {
    case 'stale': return 'Фрагмент устарел. Обновите требования и откройте ссылку снова.';
    case 'superseded': return 'Версия расшифровки заменена.';
    case 'review_required': return 'Публикация ожидает проверки человеком.';
    case 'partial': return 'Фрагмент частичной расшифровки сейчас недоступен.';
    case 'permission_denied': return 'Доступ к записи отозван или запрещён.';
    default: return 'Расшифровка временно недоступна.';
  }
}

function reviewStatusLabel(decision: TranscriptReviewDecision): string {
  switch (decision) {
    case 'approve': return 'Версия ASR одобрена';
    case 'correct': return 'Создана исправленная версия';
    case 'reject': return 'Версия ASR отклонена';
  }
}
