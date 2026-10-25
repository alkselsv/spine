import type { components } from '../../../api/generated/schema';

import { ru } from '../../i18n/resources';
import styles from './SessionBrowserPage.module.css';

export type Completeness = components['schemas']['AnswerCompletenessPresentation'];

export function CompletenessPresentation({ value }: { value: Completeness | null }) {
  if (!value) return null;
  return (
    <section
      aria-label={ru.chat.completeness.title}
      className={styles.completenessDecision}
      data-conflict={value.conflict_status}
      data-partiality={value.partiality_status}
    >
      <strong>{partialityLabel(value.partiality_status)}</strong>
      <span>{ru.chat.completeness.coverage}: {coverageLabel(value.coverage_status)}</span>
      <span>{ru.chat.completeness.conflict}: {conflictLabel(value.conflict_status)}</span>
      <span>{ru.chat.completeness.fallback}: {fallbackLabel(value.partial_fallback_authority)}</span>
      <span>{ru.chat.completeness.serving}: {servingLabel(value.serving_decision)}</span>
      <span>{ru.chat.completeness.validation}: {validationLabel(value.validation_decision)}</span>
    </section>
  );
}

function partialityLabel(value: Completeness['partiality_status']): string {
  return ru.chat.completeness.partiality[value];
}

function coverageLabel(value: Completeness['coverage_status']): string {
  return ru.chat.completeness.coverageStates[value];
}

function conflictLabel(value: Completeness['conflict_status']): string {
  return ru.chat.completeness.conflictStates[value];
}

function fallbackLabel(value: Completeness['partial_fallback_authority']): string {
  return value ? ru.chat.completeness.fallbackAuthorities[value] : ru.chat.completeness.notAuthorized;
}

function servingLabel(value: Completeness['serving_decision']): string {
  return ru.chat.completeness.servingDecisions[value];
}

function validationLabel(value: Completeness['validation_decision']): string {
  return ru.chat.completeness.validationDecisions[value];
}
