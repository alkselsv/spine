import type { components } from '../../../api/generated/schema';

import { ru } from '../../i18n/resources';
import {
  diagnosticAction,
  freshnessLabel,
  healthLabel,
  lifecycleLabel,
  processingStateLabel,
  publicationStatusLabel,
} from './documentLabels';
import styles from './DocumentAccessMetadata.module.css';

type DocumentOperationalMetadata = components['schemas']['DocumentOperationalMetadata'];
type PublicationSnapshot = components['schemas']['PublicationSnapshot'];
type Scope = { environmentId: string; workspaceId: string };

export type DocumentMetadataState = 'available' | 'degraded' | 'stale' | 'unavailable';

export function documentMetadataState(
  metadata: DocumentOperationalMetadata | null | undefined,
): DocumentMetadataState {
  if (!metadata || metadata.status === 'unavailable') return 'unavailable';
  if (metadata.status === 'degraded' || metadata.health === 'degraded' || metadata.diagnostic_code) {
    return 'degraded';
  }
  if (metadata.freshness === 'stale') return 'stale';
  return 'available';
}

export function DocumentAccessMetadata({
  metadata,
  publication,
  scope,
  traceId,
  compact = false,
}: {
  metadata: DocumentOperationalMetadata | null | undefined;
  publication?: PublicationSnapshot | null;
  scope?: Scope;
  traceId?: string;
  compact?: boolean;
}) {
  const effectiveMetadata = metadata
    ? {
        ...metadata,
        freshness: metadata.freshness ?? publication?.freshness,
        health: metadata.health ?? publication?.health,
      }
    : metadata;
  const state = documentMetadataState(effectiveMetadata);
  const policy = metadata?.access_policy;
  const securityDomain = metadata?.security_domain;
  const action = metadataAction(effectiveMetadata);

  if (compact) {
    return (
      <div
        aria-label={ru.documents.accessMetadataTitle}
        className={`${styles.panel} ${styles.compact}`}
        role="group"
      >
        <span className={`${styles.status} ${styles[`status_${state}`]}`}>
          {metadataStatusLabel(effectiveMetadata)}
        </span>
        <CompactValue
          label={ru.documents.policyVersion}
          value={policy ? `v${policy.version}` : ru.documents.unknown}
        />
        <CompactValue
          label={ru.documents.policyGeneration}
          value={policy ? String(policy.generation) : ru.documents.unknown}
        />
        <CompactValue
          label={ru.documents.policyVersionId}
          value={policy?.policy_version_id ?? ru.documents.unknown}
          code={Boolean(policy)}
        />
        <CompactValue
          label={ru.documents.securityDomainReference}
          value={securityDomain?.reference ?? ru.documents.securityDomainUnavailable}
        />
        <CompactValue
          label={ru.documents.securityDomainVersion}
          value={securityDomain?.version ?? ru.documents.unknown}
        />
        <CompactValue
          label={ru.documents.securityDomainBinding}
          value={securityDomain ? String(securityDomain.binding_version) : ru.documents.unknown}
        />
        <CompactValue label={ru.documents.lifecycle} value={lifecycleLabel(effectiveMetadata?.lifecycle)} />
        <CompactValue
          label={ru.documents.processingState}
          value={processingStateLabel(effectiveMetadata?.processing_state)}
        />
        <CompactValue
          label={ru.documents.publication}
          value={publicationStatusLabel(effectiveMetadata?.publication_status)}
        />
        <CompactValue label={ru.documents.freshness} value={freshnessLabel(effectiveMetadata?.freshness)} />
        <CompactValue label={ru.documents.projectionHealth} value={healthLabel(effectiveMetadata?.health)} />
        <CompactValue
          label={ru.documents.metadataRevision}
          value={metadata?.source_revision_id ?? ru.documents.unknown}
          code={Boolean(metadata?.source_revision_id)}
        />
        {scope ? <CompactValue label={ru.documents.scopeWorkspace} value={scope.workspaceId} code /> : null}
        {scope ? <CompactValue label={ru.documents.scopeEnvironment} value={scope.environmentId} code /> : null}
        {traceId ? <CompactValue label={ru.documents.responseTrace} value={traceId} code /> : null}
        {action ? <CompactValue label={ru.documents.nextAction} value={action} /> : null}
      </div>
    );
  }

  return (
    <section aria-labelledby="document-access-metadata-heading" className={styles.panel}>
      <header className={styles.header}>
        <div>
          <h3 id="document-access-metadata-heading">{ru.documents.accessMetadataTitle}</h3>
          <p>{ru.documents.accessMetadataDescription}</p>
        </div>
        <span className={`${styles.status} ${styles[`status_${state}`]}`}>
          {metadataStatusLabel(effectiveMetadata)}
        </span>
      </header>

      <dl className={styles.details}>
        {scope ? (
          <>
            <MetadataValue label={ru.documents.scopeWorkspace} value={scope.workspaceId} code />
            <MetadataValue label={ru.documents.scopeEnvironment} value={scope.environmentId} code />
          </>
        ) : null}
        <MetadataValue label={ru.documents.metadataStatus} value={metadataStatusLabel(effectiveMetadata)} />
        <MetadataValue label={ru.documents.lifecycle} value={lifecycleLabel(effectiveMetadata?.lifecycle)} />
        <MetadataValue
          label={ru.documents.processingState}
          value={processingStateLabel(effectiveMetadata?.processing_state)}
        />
        <MetadataValue
          label={ru.documents.publication}
          value={publicationStatusLabel(effectiveMetadata?.publication_status)}
        />
        <MetadataValue label={ru.documents.freshness} value={freshnessLabel(effectiveMetadata?.freshness)} />
        <MetadataValue label={ru.documents.projectionHealth} value={healthLabel(effectiveMetadata?.health)} />
        <MetadataValue
          label={ru.documents.policyVersion}
          value={policy ? `v${policy.version}` : ru.documents.unknown}
        />
        <MetadataValue
          label={ru.documents.policyGeneration}
          value={policy ? String(policy.generation) : ru.documents.unknown}
        />
        <MetadataValue
          label={ru.documents.policyVersionId}
          value={policy?.policy_version_id ?? ru.documents.unknown}
          code={Boolean(policy)}
        />
        <MetadataValue
          label={ru.documents.securityDomainReference}
          value={securityDomain?.reference ?? ru.documents.securityDomainUnavailable}
        />
        <MetadataValue
          label={ru.documents.securityDomainVersion}
          value={securityDomain?.version ?? ru.documents.unknown}
        />
        <MetadataValue
          label={ru.documents.securityDomainBinding}
          value={securityDomain ? String(securityDomain.binding_version) : ru.documents.unknown}
        />
        <MetadataValue
          label={ru.documents.metadataRevision}
          value={metadata?.source_revision_id ?? ru.documents.unknown}
          code={Boolean(metadata?.source_revision_id)}
        />
        {traceId ? <MetadataValue label={ru.documents.responseTrace} value={traceId} code /> : null}
      </dl>

      {action ? (
        <p className={styles.nextAction} role="note">
          <strong>{ru.documents.nextAction}:</strong> {action}
        </p>
      ) : null}
    </section>
  );
}

function CompactValue({
  label,
  value,
  code = false,
}: {
  label: string;
  value: string;
  code?: boolean;
}) {
  return (
    <span className={styles.compactValue}>
      <strong>{label}:</strong> {code ? <code>{value}</code> : value}
    </span>
  );
}

function MetadataValue({
  label,
  value,
  code = false,
}: {
  label: string;
  value: string;
  code?: boolean;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{code ? <code>{value}</code> : value}</dd>
    </div>
  );
}

function metadataStatusLabel(metadata: DocumentOperationalMetadata | null | undefined) {
  if (!metadata) return ru.documents.states.unavailable;
  const state = documentMetadataState(metadata);
  if (state === 'stale') return ru.documents.states.stale;
  if (state === 'degraded') return ru.documents.filterOptions.publication_health.degraded;
  if (state === 'available') return ru.documents.available;
  return ru.documents.states.unavailable;
}

function metadataAction(metadata: DocumentOperationalMetadata | null | undefined) {
  if (metadata?.diagnostic_code) return diagnosticAction(metadata.diagnostic_code);
  if (!metadata || metadata.status === 'unavailable') return ru.documents.currentStateAction;
  if (metadata.freshness === 'stale' || metadata.health === 'degraded' || metadata.status === 'degraded') {
    return ru.documents.retryAction;
  }
  return null;
}
