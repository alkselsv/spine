export const REQUIRED_FORMATS = ['pdf', 'docx', 'xlsx', 'markdown', 'text'] as const;
export const REQUIRED_SCENARIOS = [
  'five_format_upload',
  'duplicate_idempotent_replay',
  'changed_content_new_revision',
  'tombstone_fences_disclosure',
  'parser_failure_retry',
  'original_storage_failure_retry',
  'post_write_pre_acceptance_failure_retry',
  'workspace_environment_isolation',
  'safe_ui_states',
] as const;
export const REQUIRED_UI_STATES = [
  'running',
  'partial',
  'retryable_failure',
  'accepted',
  'stale',
  'deleted',
] as const;

export type QualificationFormat = typeof REQUIRED_FORMATS[number];
export type QualificationScenario = typeof REQUIRED_SCENARIOS[number];
export type QualificationUiState = typeof REQUIRED_UI_STATES[number];

export interface QualificationDeployment {
  api_release_identity: string;
  authorization_generation: number;
  commit_sha: string;
  deployment_fingerprint: string;
  provider_profile_identity: string;
  qualification_run_id: string;
  web_release_identity: string;
}

export interface FormatObservation {
  accepted: true;
  checksum_reference: string;
  format: QualificationFormat;
  locator_kinds: string[];
  original_receipt_digest: string;
  original_receipt_reference: string;
  processing_receipt_reference: string;
  provenance_reference: string;
  revision_reference: string;
  source_reference: string;
}

export interface AttemptedObservation {
  format: QualificationFormat;
  processing_receipt_reference: string;
  revision_reference: string;
  source_reference: string;
}

export interface FailureObservation {
  accepted_before_retry: boolean;
  failure_reference: string;
  initial_run_reference: string;
  logical_effect_count: number;
  reason_code: string;
  retry_passed: boolean;
  retry_operation_reference?: string;
  retry_replay: boolean;
}

export interface IngestionQualificationReport {
  schema_version: 'issue-561-five-format-ingestion:v1';
  ticket: 561;
  status: 'pass' | 'blocked' | 'not_verified' | 'fail';
  reason_code: string | null;
  required_formats: readonly QualificationFormat[];
  required_scenarios: readonly QualificationScenario[];
  formats: QualificationFormat[];
  scenarios: QualificationScenario[];
  ui_states: QualificationUiState[];
  q16_01_compatibility: 'pass' | 'not_verified';
  authorization: {
    corpus_approval: 'pass' | 'not_verified';
    corpus_approval_reference?: string;
    trusted_upload_authority: 'pass' | 'not_verified';
    trusted_upload_authority_reference?: string;
  };
  deployment: QualificationDeployment;
  format_observations: FormatObservation[];
  attempted_observations: AttemptedObservation[];
  lifecycle: {
    duplicate?: {
      logical_effect_count: number;
      replay: boolean;
      result_stable: boolean;
      revision_reference: string;
      run_reference: string;
      source_reference: string;
    };
    changed_revision?: {
      new_revision_reference: string;
      predecessor_preserved: boolean;
      predecessor_revision_reference: string;
      processing_receipt_reference: string;
      replay: boolean;
      result_stable: boolean;
      source_reference: string;
    };
    tombstone?: {
      disclosure_fenced: boolean;
      history_preserved: boolean;
      operation_reference: string;
      tombstone_revision_reference: string;
    };
  };
  failures: {
    parser?: FailureObservation;
    original_storage?: FailureObservation;
    post_write_pre_acceptance?: FailureObservation;
  };
  isolation: {
    denied_environment_upload?: 'pass';
    denied_environment_status_read?: 'pass';
    denied_workspace_upload?: 'pass';
    denied_workspace_status_read?: 'pass';
    protected_metadata_disclosed?: boolean;
    combined_foreign_status?: number;
    cross_environment_upload_status?: number;
  };
  execution: {
    api_mocking: false;
    browser: 'chromium';
    cli_user_actions: false;
    controlled_fixture: false;
    route_fulfillment: false;
  };
  privacy: {
    content_fields_recorded: false;
    credentials_recorded: false;
    filenames_recorded: false;
    metadata_only: true;
    raw_errors_recorded: false;
    screenshots: false;
    trace: false;
    video: false;
  };
  blockers: number[];
  unverified_scenarios: QualificationScenario[];
}

export function createIngestionQualificationReport(
  deployment: QualificationDeployment,
): IngestionQualificationReport {
  return {
    schema_version: 'issue-561-five-format-ingestion:v1',
    ticket: 561,
    status: 'blocked',
    reason_code: 'execution_not_started',
    required_formats: REQUIRED_FORMATS,
    required_scenarios: REQUIRED_SCENARIOS,
    formats: [],
    scenarios: [],
    ui_states: [],
    q16_01_compatibility: 'not_verified',
    authorization: { corpus_approval: 'not_verified', trusted_upload_authority: 'not_verified' },
    deployment,
    format_observations: [],
    attempted_observations: [],
    lifecycle: {},
    failures: {},
    isolation: {},
    execution: {
      api_mocking: false,
      browser: 'chromium',
      cli_user_actions: false,
      controlled_fixture: false,
      route_fulfillment: false,
    },
    privacy: {
      content_fields_recorded: false,
      credentials_recorded: false,
      filenames_recorded: false,
      metadata_only: true,
      raw_errors_recorded: false,
      screenshots: false,
      trace: false,
      video: false,
    },
    blockers: [],
    unverified_scenarios: [...REQUIRED_SCENARIOS],
  };
}

export function finalizeIngestionQualificationReport(report: IngestionQualificationReport): void {
  const observedFormats = new Set(report.format_observations
    .filter((observation) => observation.accepted)
    .map((observation) => observation.format));
  const observedUiStates = new Set(report.ui_states);
  const isolation = report.isolation;
  const scenarios = new Set<QualificationScenario>();
  if (REQUIRED_FORMATS.every((format) => observedFormats.has(format))) scenarios.add('five_format_upload');
  if (report.lifecycle.duplicate?.logical_effect_count === 1
    && report.lifecycle.duplicate.replay
    && report.lifecycle.duplicate.result_stable) scenarios.add('duplicate_idempotent_replay');
  if (report.lifecycle.changed_revision?.predecessor_preserved
    && report.lifecycle.changed_revision.replay
    && report.lifecycle.changed_revision.result_stable) scenarios.add('changed_content_new_revision');
  if (report.lifecycle.tombstone?.disclosure_fenced && report.lifecycle.tombstone.history_preserved) {
    scenarios.add('tombstone_fences_disclosure');
  }
  if (failurePassed(report.failures.parser, true)) scenarios.add('parser_failure_retry');
  if (failurePassed(report.failures.original_storage, true)) scenarios.add('original_storage_failure_retry');
  if (failurePassed(report.failures.post_write_pre_acceptance, false)) {
    scenarios.add('post_write_pre_acceptance_failure_retry');
  }
  if (
    isolation.denied_environment_upload === 'pass'
    && isolation.denied_environment_status_read === 'pass'
    && isolation.denied_workspace_upload === 'pass'
    && isolation.denied_workspace_status_read === 'pass'
    && isolation.protected_metadata_disclosed === false
  ) scenarios.add('workspace_environment_isolation');
  if (REQUIRED_UI_STATES.every((state) => observedUiStates.has(state))) scenarios.add('safe_ui_states');

  report.scenarios = REQUIRED_SCENARIOS.filter((scenario) => scenarios.has(scenario));
  report.formats = REQUIRED_FORMATS.filter((format) => observedFormats.has(format));
  report.unverified_scenarios = REQUIRED_SCENARIOS.filter((scenario) => !scenarios.has(scenario));
  const complete = report.unverified_scenarios.length === 0
    && report.q16_01_compatibility === 'pass'
    && report.authorization.corpus_approval === 'pass'
    && Boolean(report.authorization.corpus_approval_reference)
    && report.authorization.trusted_upload_authority === 'pass'
    && Boolean(report.authorization.trusted_upload_authority_reference);
  report.status = complete ? 'pass' : 'not_verified';
  report.reason_code = complete ? null : 'remaining_lifecycle_edges_not_exercised';
}

export function requireCompleteIngestionQualificationReport(
  report: IngestionQualificationReport,
): void {
  finalizeIngestionQualificationReport(report);
  if (report.status !== 'pass') throw new Error(report.reason_code ?? 'ingestion_qualification_incomplete');
}

function failurePassed(
  observation: FailureObservation | undefined,
  acceptedBeforeRetry: boolean,
): boolean {
  if (!observation) return false;
  return observation.accepted_before_retry === acceptedBeforeRetry
    && observation.logical_effect_count === 1
    && observation.retry_passed === true
    && observation.retry_replay === true;
}
