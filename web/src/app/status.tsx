import { ru } from '../i18n/resources';
import { localizeApiError } from './errors';
import styles from './AppShell.module.css';

export type InterfaceState =
  | 'loading'
  | 'empty'
  | 'partial'
  | 'stale'
  | 'degraded'
  | 'permissionDenied'
  | 'validationFailed'
  | 'failed'
  | 'ready';

export function StatusPanel({ state }: { state: InterfaceState }) {
  const copy = ru.status[state];
  return (
    <div
      aria-label={copy.title}
      className={`${styles.statusPanel} ${styles[`statusPanel_${state}`]}`}
      role="region"
    >
      <strong>{copy.title}</strong>
      <span>{copy.description}</span>
    </div>
  );
}

export function ApiErrorPanel({ error }: { error: unknown }) {
  const copy = localizeApiError(error);
  return (
    <div
      aria-label={copy.title}
      className={`${styles.statusPanel} ${styles[`statusPanel_${copy.state}`]}`}
      role="alert"
    >
      <strong>{copy.title}</strong>
      <span>{copy.description}</span>
      {copy.traceId ? <small>Код обращения: {copy.traceId}</small> : null}
    </div>
  );
}
