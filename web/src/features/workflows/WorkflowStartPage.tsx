import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';

import { createScopedApiClient, SpineApiError } from '../../api/client';
import { useAuth } from '../../app/auth';
import { useScope } from '../../app/scope';
import { StatusPanel } from '../../app/status';
import styles from '../chat/SessionBrowserPage.module.css';

type AcceptedSource = {
  source_object_id: string;
  source_revision_id: string;
  provenance_id: string;
};

export function WorkflowStartPage() {
  const auth = useAuth();
  const { scope } = useScope();
  const navigate = useNavigate();
  const client = useMemo(
    () => createScopedApiClient({
      baseUrl: window.location.origin,
      getBearerCredential: auth.getBearerCredential ?? (() => ''),
      scope,
    }),
    [auth.getBearerCredential, scope],
  );
  const [message, setMessage] = useState('');
  const [source, setSource] = useState<AcceptedSource | null>(null);
  const [sourceFields, setSourceFields] = useState<AcceptedSource>({
    source_object_id: '',
    source_revision_id: '',
    provenance_id: '',
  });
  const [showSource, setShowSource] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const commandRef = useRef<{ idempotencyKey: string; workItemKey: string; message: string; source: AcceptedSource | null } | null>(null);

  if (!auth.getBearerCredential) return <StatusPanel state="permissionDenied" />;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting || !message.trim()) return;
    const command = commandRef.current ?? {
      idempotencyKey: crypto.randomUUID(),
      workItemKey: crypto.randomUUID(),
      message: message.trim(),
      source,
    };
    commandRef.current = command;
    setSubmitting(true);
    setError(null);
    try {
      const { data, error: responseError } = await client.POST('/api/v1/runs', {
        params: { header: { 'Idempotency-Key': command.idempotencyKey } },
        body: {
          schema_version: 1,
          workflow_key: 'commercial.requirements',
          work_item_key: command.workItemKey,
          message: command.message,
          accepted_sources: command.source ? [command.source] : [],
        },
      });
      if (responseError || !data) {
        setError(responseError instanceof SpineApiError ? responseError.safeMessage : 'Не удалось запустить работу. Повторите запрос.');
        return;
      }
      commandRef.current = null;
      navigate(`/workflow/runs/${data.run_id}`);
    } catch (requestError) {
      setError(requestError instanceof SpineApiError ? requestError.safeMessage : 'Не удалось запустить работу. Повторите запрос.');
    } finally {
      setSubmitting(false);
    }
  }

  function updateSource(field: keyof AcceptedSource, value: string) {
    commandRef.current = null;
    const next = { ...sourceFields, [field]: value };
    setSourceFields(next);
    if (Object.values(next).every((item) => item.length > 0)) setSource(next);
    else setSource(null);
  }

  function toggleSource() {
    commandRef.current = null;
    const nextVisible = !showSource;
    setShowSource(nextVisible);
    setSource(
      nextVisible && Object.values(sourceFields).every((item) => item.length > 0)
        ? sourceFields
        : null,
    );
  }

  return (
    <section aria-labelledby="workflow-start-heading" className={styles.page}>
      <div className={styles.pageHeader}>
        <h2 id="workflow-start-heading">Новая работа</h2>
        <p>Опишите запрос первым сообщением. Spine сохранит его в запуске и восстановит состояние при следующем открытии.</p>
      </div>
      <form className={styles.askForm} onSubmit={(event) => void submit(event)}>
        <label htmlFor="workflow-request-message">Сообщение</label>
        <textarea
          id="workflow-request-message"
          autoComplete="off"
          disabled={submitting}
          onChange={(event) => {
            setMessage(event.target.value);
            commandRef.current = null;
          }}
          placeholder="Например: подготовь требования для коммерческого предложения…"
          value={message}
        />
        <button
          className={styles.textButton}
          onClick={toggleSource}
          type="button"
        >
          {showSource ? 'Убрать ссылку на источник' : 'Добавить принятую версию источника'}
        </button>
        {showSource ? (
          <fieldset>
            <legend>Принятая версия источника</legend>
            {([
              ['source_object_id', 'Source Object ID'],
              ['source_revision_id', 'Source Revision ID'],
              ['provenance_id', 'Provenance ID'],
            ] as const).map(([field, label]) => (
              <label key={field} htmlFor={`workflow-${field}`}>
                {label}
                <input
                  id={`workflow-${field}`}
                  onChange={(event) => updateSource(field, event.target.value.trim())}
                  value={sourceFields[field]}
                />
              </label>
            ))}
            {source ? <p role="status">Ссылка будет проверена сервером в текущей рабочей области.</p> : null}
          </fieldset>
        ) : null}
        <div className={styles.askActions}>
          <button className={styles.primaryButton} disabled={submitting || !message.trim()} type="submit">
            {submitting ? 'Запускаем…' : 'Начать работу'}
          </button>
        </div>
        {submitting ? <p role="status">Сохраняем запрос и создаём запуск…</p> : null}
        {error ? <p role="alert">{error}</p> : null}
      </form>
    </section>
  );
}
