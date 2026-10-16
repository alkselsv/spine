import type { components } from '../../../api/generated/schema';

import { ru } from '../../i18n/resources';

export function lifecycleLabel(value: components['schemas']['RevisionLifecycle'] | null | undefined) {
  if (value === 'observed') return ru.documents.filterOptions.lifecycle.observed;
  if (value === 'quarantined') return ru.documents.filterOptions.lifecycle.quarantined;
  if (value === 'rejected') return ru.documents.filterOptions.lifecycle.rejected;
  if (value === 'superseded') return ru.documents.filterOptions.lifecycle.superseded;
  if (value === 'current') return ru.documents.filterOptions.lifecycle.current;
  if (value === 'tombstoned') return ru.documents.filterOptions.lifecycle.tombstoned;
  if (value === 'processing') return ru.documents.filterOptions.lifecycle.processing;
  return ru.documents.unknown;
}

export function processingStateLabel(value: components['schemas']['IngestionItemState'] | null | undefined) {
  if (value === 'received') return ru.documents.filterOptions.processing_state.received;
  if (value === 'stored') return ru.documents.filterOptions.processing_state.stored;
  if (value === 'observed') return ru.documents.filterOptions.processing_state.observed;
  if (value === 'admission_pending') return ru.documents.filterOptions.processing_state.admission_pending;
  if (value === 'quarantined') return ru.documents.filterOptions.processing_state.quarantined;
  if (value === 'rejected') return ru.documents.filterOptions.processing_state.rejected;
  if (value === 'accepted') return ru.documents.filterOptions.processing_state.accepted;
  if (value === 'processing') return ru.documents.filterOptions.processing_state.processing;
  if (value === 'processed') return ru.documents.filterOptions.processing_state.processed;
  if (value === 'processing_failed') return ru.documents.filterOptions.processing_state.processing_failed;
  if (value === 'projection_requested') return ru.documents.filterOptions.processing_state.projection_requested;
  return ru.documents.unknown;
}

export function publicationStatusLabel(value: components['schemas']['DocumentPublicationStatus'] | null | undefined) {
  if (value === 'published') return ru.documents.filterOptions.publication_status.published;
  if (value === 'unpublished') return ru.documents.filterOptions.publication_status.unpublished;
  if (value === 'unavailable') return ru.documents.filterOptions.publication_status.unavailable;
  return ru.documents.unknown;
}

export function freshnessLabel(value: components['schemas']['ProjectionFreshness'] | null | undefined) {
  if (value === 'current') return ru.documents.filterOptions.publication_health.healthy;
  if (value === 'stale') return ru.documents.states.stale;
  return ru.documents.unknown;
}

export function healthLabel(value: components['schemas']['ProjectionHealth'] | null | undefined) {
  if (value === 'healthy') return ru.documents.filterOptions.publication_health.healthy;
  if (value === 'degraded') return ru.documents.filterOptions.publication_health.degraded;
  return ru.documents.unknown;
}

export function diagnosticAction(value: components['schemas']['OperationalMetadataDiagnostic']) {
  if (value === 'current_state_unavailable') return ru.documents.currentStateAction;
  if (value === 'policy_unavailable') return ru.documents.policyAction;
  if (value === 'security_domain_unavailable') return ru.documents.securityDomainAction;
  return ru.documents.noAction;
}
