import { describe, expect, it } from 'vitest';

import {
  createIngestionQualificationReport,
  finalizeIngestionQualificationReport,
  requireCompleteIngestionQualificationReport,
  type IngestionQualificationReport,
} from './ingestion-qualification-report';

const DEPLOYMENT = {
  api_release_identity: 'api-v1',
  authorization_generation: 17,
  commit_sha: 'a'.repeat(40),
  deployment_fingerprint: 'b'.repeat(64),
  provider_profile_identity: 'provider-local-8935297e3def5e',
  qualification_run_id: 'issue561-run-1',
  web_release_identity: 'web-v1',
} as const;

describe('ingestion qualification report', () => {
  it('test_complete_live_evidence_produces_pass_report', () => {
    const report = completeReport();

    finalizeIngestionQualificationReport(report);

    expect(report.status).toBe('pass');
    expect(report.reason_code).toBeNull();
    expect(report.unverified_scenarios).toEqual([]);
  });

  it('test_missing_lifecycle_edge_remains_not_verified', () => {
    const report = completeReport();
    delete report.failures.original_storage;

    finalizeIngestionQualificationReport(report);

    expect(report.status).toBe('not_verified');
    expect(report.reason_code).toBe('remaining_lifecycle_edges_not_exercised');
    expect(report.unverified_scenarios).toEqual(['original_storage_failure_retry']);
  });

  it('test_missing_lifecycle_edge_fails_the_live_qualification', () => {
    const report = completeReport();
    delete report.failures.original_storage;

    expect(() => requireCompleteIngestionQualificationReport(report))
      .toThrowError('remaining_lifecycle_edges_not_exercised');
    expect(report.status).toBe('not_verified');
  });

  it('test_unreferenced_approval_cannot_produce_pass_report', () => {
    const report = completeReport();
    delete report.authorization.corpus_approval_reference;

    finalizeIngestionQualificationReport(report);

    expect(report.status).toBe('not_verified');
  });
});

function completeReport(): IngestionQualificationReport {
  const report = createIngestionQualificationReport(DEPLOYMENT);
  report.q16_01_compatibility = 'pass';
  report.authorization = {
    corpus_approval: 'pass',
    corpus_approval_reference: 'corpus-approval-42',
    trusted_upload_authority: 'pass',
    trusted_upload_authority_reference: 'authority-approval-17',
  };
  report.formats.push('pdf', 'docx', 'xlsx', 'markdown', 'text');
  report.ui_states.push('running', 'partial', 'retryable_failure', 'accepted', 'stale', 'deleted');
  report.format_observations.push(
    ...report.required_formats.map((format) => ({
      accepted: true as const,
      checksum_reference: 'c'.repeat(64),
      format,
      locator_kinds: ['stable_locator'],
      processing_receipt_reference: '10000000-0000-4000-8000-000000000001',
      revision_reference: '20000000-0000-4000-8000-000000000001',
      source_reference: '30000000-0000-4000-8000-000000000001',
    })),
  );
  report.lifecycle.duplicate = {
    logical_effect_count: 1,
    replay: true,
    result_stable: true,
    revision_reference: '20000000-0000-4000-8000-000000000001',
    run_reference: '40000000-0000-4000-8000-000000000001',
    source_reference: '30000000-0000-4000-8000-000000000001',
  };
  report.lifecycle.changed_revision = {
    new_revision_reference: '20000000-0000-4000-8000-000000000002',
    predecessor_preserved: true,
    predecessor_revision_reference: '20000000-0000-4000-8000-000000000001',
    processing_receipt_reference: '10000000-0000-4000-8000-000000000001',
    replay: true,
    result_stable: true,
    source_reference: '30000000-0000-4000-8000-000000000001',
  };
  report.lifecycle.tombstone = {
    disclosure_fenced: true,
    history_preserved: true,
    operation_reference: '50000000-0000-4000-8000-000000000001',
    tombstone_revision_reference: '20000000-0000-4000-8000-000000000003',
  };
  report.failures.parser = passedFailure('parser_retryable', true);
  report.failures.original_storage = passedFailure('original_storage_unavailable', true);
  report.failures.post_write_pre_acceptance = passedFailure('pre_acceptance_retryable', false);
  report.isolation = {
    denied_environment_upload: 'pass',
    denied_environment_status_read: 'pass',
    denied_workspace_upload: 'pass',
    denied_workspace_status_read: 'pass',
    protected_metadata_disclosed: false,
  };
  return report;
}

function passedFailure(reasonCode: string, acceptedBeforeRetry: boolean) {
  return {
    accepted_before_retry: acceptedBeforeRetry,
    failure_reference: '60000000-0000-4000-8000-000000000001',
    initial_run_reference: '40000000-0000-4000-8000-000000000001',
    logical_effect_count: 1,
    reason_code: reasonCode,
    retry_passed: true,
    retry_replay: true,
  };
}
