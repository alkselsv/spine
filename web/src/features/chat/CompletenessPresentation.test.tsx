import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { components } from '../../../api/generated/schema';
import { ru } from '../../i18n/resources';
import { CompletenessPresentation } from './CompletenessPresentation';

type Completeness = components['schemas']['AnswerCompletenessPresentation'];

const base: Completeness = {
  schema_version: 'answer-completeness-presentation:v1',
  target_scope_id: '00000000-0000-4000-8000-000000000001',
  target_scope_version: 'target-completeness-scope:v1',
  observed_coverage_id: '00000000-0000-4000-8000-000000000002',
  observed_coverage_version: 'observed-coverage:v1',
  coverage_status: 'complete',
  partiality_status: 'exhaustive',
  conflict_status: 'none',
  partial_fallback_authority: null,
  serving_decision: 'serve',
  validation_decision: 'complete',
};

describe('CompletenessPresentation', () => {
  it('test_exhaustive_answer_uses_server_decision_instead_of_evidence_count', () => {
    render(<CompletenessPresentation value={base} />);

    expect(screen.getByText(ru.chat.completeness.partiality.exhaustive)).toBeVisible();
    expect(screen.getByText(/подтверждено полностью/)).toBeVisible();
  });

  it('test_partial_authorized_conflict_renders_both_independent_states', () => {
    render(<CompletenessPresentation value={{
      ...base,
      coverage_status: 'insufficient',
      partiality_status: 'partial',
      conflict_status: 'presented',
      partial_fallback_authority: 'current_request',
      validation_decision: 'grounded_conflict',
    }} />);

    expect(screen.getByText(ru.chat.completeness.partiality.partial)).toBeVisible();
    expect(screen.getByText(/несовместимые подтверждённые позиции/)).toBeVisible();
    expect(screen.getByText(/разрешено текущим запросом/)).toBeVisible();
  });

  it('test_unproven_coverage_withholds_answer_without_scope_membership_or_counts', () => {
    const { container } = render(<CompletenessPresentation value={{
      ...base,
      coverage_status: 'unproven',
      partiality_status: 'none',
      serving_decision: 'withhold',
      validation_decision: 'coverage_not_proven',
    }} />);

    expect(screen.getByText(ru.chat.completeness.partiality.none)).toBeVisible();
    expect(screen.getByText(/покрытие не доказано/)).toBeVisible();
    expect(container).not.toHaveTextContent(base.target_scope_id);
    expect(container).not.toHaveTextContent(base.observed_coverage_id);
  });

  it('test_precedence_resolved_conflict_names_the_approved_decision_only', () => {
    render(<CompletenessPresentation value={{
      ...base,
      conflict_status: 'resolved_by_precedence',
      validation_decision: 'precedence_applied',
    }} />);

    expect(screen.getByText(/утверждённое правило приоритета/)).toBeVisible();
    expect(screen.getByText(/применено правило приоритета/)).toBeVisible();
  });

  it.each(['denied', 'revoked'])('test_%s_content_hides_protected_completeness_details', () => {
    const { container } = render(<CompletenessPresentation value={null} />);
    expect(container).toBeEmptyDOMElement();
    expect(container).not.toHaveTextContent(base.target_scope_id);
    expect(container).not.toHaveTextContent(base.observed_coverage_id);
  });
});
