import { test, type APIRequestContext, type Page } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, dirname } from 'node:path';

import {
  readIngestionQualificationConfig,
  type IngestionQualificationConfig,
  type QualificationFormat,
} from './ingestion-qualification-config';

const REQUIRED_FORMATS: QualificationFormat[] = ['pdf', 'docx', 'xlsx', 'markdown', 'text'];
const REQUIRED_SCENARIOS = [
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
const RETRY_PATH = '/api/v1/documents/processing/retry';
const TOMBSTONE_SUFFIX = '/tombstone';

test('test_real_browser_qualifies_five_format_canonical_ingestion', async ({ page, request }) => {
  test.setTimeout(45 * 60_000);
  const config = readIngestionQualificationConfig(process.env);
  const report = baseReport(config);

  try {
    await verifyQ16Compatibility(config);
    report.q16_01_compatibility = 'pass';
    await installRuntimeBinding(page, config);
    const listResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/v1/documents',
    );
    await page.goto(scoped('/knowledge/documents', config.workspaceAId, config.environmentAId));
    if ((await listResponse).status() !== 200) throw new Error('documents_page_unavailable');

    await page.locator('input[type="file"]').setInputFiles(REQUIRED_FORMATS.map((format) => config.corpus[format]));
    await page.getByRole('region', { name: 'Проверка перед отправкой' }).waitFor({ state: 'visible' });
    report.ui_states.push('running');
    const uploadResponsePromise = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/v1/documents/batches',
    );
    await page.getByRole('button', { name: 'Загрузить документы' }).click();
    const uploadResponse = await uploadResponsePromise;
    if (![200, 201, 202].includes(uploadResponse.status())) throw new Error('five_format_upload_rejected');
    const upload = record(await uploadResponse.json(), 'five_format_upload_schema_invalid');
    const runId = safeUuid(upload.run_id, 'five_format_upload_schema_invalid');
    const terminal = await waitForTerminal(request, config, runId);
    const items = arrayOfRecords(terminal.items, 'five_format_upload_schema_invalid');
    if (items.length !== REQUIRED_FORMATS.length) throw new Error('five_format_upload_incomplete');

    for (const [index, format] of REQUIRED_FORMATS.entries()) {
      const item = items.find((candidate) => candidate.input_ordinal === index + 1);
      if (!item) throw new Error('five_format_upload_incomplete');
      if (item.status !== 'succeeded' || item.acceptance !== 'accepted') {
        throw new Error('five_format_upload_not_accepted');
      }
      const sourceReference = safeUuid(item.source_object_id, 'five_format_upload_schema_invalid');
      const revisionReference = safeUuid(item.source_revision_id, 'five_format_upload_schema_invalid');
      const processingReceipt = record(item.processing_receipt, 'five_format_upload_schema_invalid');
      const processingReceiptReference = safeUuid(
        processingReceipt.receipt_id,
        'five_format_upload_schema_invalid',
      );
      report.attempted_observations.push({
        format,
        processing_receipt_reference: processingReceiptReference,
        revision_reference: revisionReference,
        source_reference: sourceReference,
      });
      const inspection = await inspectParsedContent(
        request,
        config,
        sourceReference,
        revisionReference,
        processingReceiptReference,
      );
      validateLocatorEvidence(format, inspection.items);
      report.format_observations.push({
        accepted: true,
        checksum_reference: safeDigest(inspection.original_sha256, 'parsed_content_checksum_unavailable'),
        format,
        locator_kinds: locatorKinds(inspection.items),
        processing_receipt_reference: processingReceiptReference,
        revision_reference: revisionReference,
        source_reference: sourceReference,
      });
      report.formats.push(format);
    }
    report.scenarios.push('five_format_upload');
    report.ui_states.push('accepted');
    report.authorization.trusted_upload_authority = 'pass';

    await qualifyDuplicateReplay(request, config, report);
    const parserFailureRunId = await qualifyParserFailureAndRetry(request, config, report);
    await qualifyIsolation(request, config, parserFailureRunId, report);
    if (report.scenarios.includes('parser_failure_retry')) {
      await qualifySafeFailureUi(page, config, parserFailureRunId, report);
    }
    await qualifyTombstone(page, request, config, report);
    report.status = 'not_verified';
    report.reason_code = 'remaining_lifecycle_edges_not_exercised';
    report.unverified_scenarios = REQUIRED_SCENARIOS.filter(
      (scenario) => !report.scenarios.includes(scenario),
    );
    await writeReport(config.reportPath, report);
  } catch (error) {
    const reason = safeReason(error);
    report.status = failureStatus(reason);
    report.reason_code = reason;
    await writeReport(config.reportPath, report);
    throw new Error('issue561_ingestion_qualification_blocked');
  }
});

async function verifyQ16Compatibility(config: IngestionQualificationConfig): Promise<void> {
  let report: Record<string, unknown>;
  try {
    report = record(JSON.parse(await readFile(config.q16ReportPath, 'utf8')), 'q16_01_report_invalid');
  } catch {
    throw new Error('q16_01_exact_target_not_qualified');
  }
  const target = record(report.target, 'q16_01_exact_target_not_qualified');
  const manifest = record(report.manifest, 'q16_01_exact_target_not_qualified');
  const providerDigest = safeDigest(
    manifest.provider_profile_digest,
    'q16_01_exact_target_not_qualified',
  );
  if (
    report.schema_version !== 'q16-01-runtime-readiness:v1'
    || report.issue !== 560
    || report.decision !== 'PASS'
    || target.commit_sha !== config.commitSha
    || target.deployment_fingerprint !== config.deploymentFingerprint
    || target.api_release_identity !== config.apiReleaseIdentity
    || target.web_release_identity !== config.webReleaseIdentity
    || target.workspace_id !== config.workspaceAId
    || target.environment_id !== config.environmentAId
    || target.authorization_generation !== config.authorizationGeneration
    || safeCode(manifest.provider_profile_id, 'q16_01_exact_target_not_qualified')
      !== 'cognee-approved-production'
    || config.providerProfileIdentity !== `provider-local-${providerDigest.slice(0, 16)}`
  ) throw new Error('q16_01_exact_target_not_qualified');
}

async function qualifyDuplicateReplay(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  report: ReturnType<typeof baseReport>,
): Promise<void> {
  const path = config.corpus.text;
  const key = `issue561-duplicate-${config.qualificationRunId}`;
  const first = await submitBatch(request, config, [path], key);
  const second = await submitBatch(request, config, [path], key);
  const firstRunId = safeUuid(first.run_id, 'duplicate_replay_schema_invalid');
  const secondRunId = safeUuid(second.run_id, 'duplicate_replay_schema_invalid');
  if (firstRunId !== secondRunId || second.replay !== true) throw new Error('duplicate_replay_not_stable');
  const terminal = await waitForTerminal(request, config, firstRunId);
  const item = arrayOfRecords(terminal.items, 'duplicate_replay_schema_invalid')[0];
  if (!item || item.status !== 'succeeded' || item.acceptance !== 'accepted') {
    throw new Error('duplicate_replay_not_accepted');
  }
  report.lifecycle.duplicate = {
    replay: true,
    run_reference: firstRunId,
    source_reference: safeUuid(item.source_object_id, 'duplicate_replay_schema_invalid'),
    revision_reference: safeUuid(item.source_revision_id, 'duplicate_replay_schema_invalid'),
  };
  report.scenarios.push('duplicate_idempotent_replay');
}

async function qualifyParserFailureAndRetry(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  report: ReturnType<typeof baseReport>,
): Promise<string> {
  const key = `issue561-parser-failure-${config.qualificationRunId}`;
  const upload = await submitBatch(request, config, [config.malformedPdfPath], key);
  const runId = safeUuid(upload.run_id, 'parser_failure_schema_invalid');
  const terminal = await waitForTerminal(request, config, runId);
  const item = arrayOfRecords(terminal.items, 'parser_failure_schema_invalid')[0];
  report.failures.parser_attempt = {
    acceptance: safeAcceptance(item?.acceptance, 'parser_failure_schema_invalid'),
    failures: item ? arrayOfRecords(item.failures, 'parser_failure_schema_invalid').map((failure) => ({
      code: safeCode(failure.code, 'parser_failure_schema_invalid'),
      retryable: safeBoolean(failure.retryable, 'parser_failure_schema_invalid'),
      stage: safeCode(failure.stage, 'parser_failure_schema_invalid'),
    })) : [],
    retryable: safeBoolean(item?.retryable, 'parser_failure_schema_invalid'),
    run_reference: runId,
    status: safeIngestionStatus(item?.status, 'parser_failure_schema_invalid'),
  };
  if (!item || item.acceptance === 'accepted') {
    throw new Error('parser_failure_false_acceptance');
  }
  if (item.retryable !== true || item.status !== 'failed') {
    report.failures.parser_attempt.unverified_reason = 'retryable_parser_failure_not_injectable';
    return runId;
  }
  const failures = arrayOfRecords(item.failures, 'parser_failure_schema_invalid');
  const failure = failures[0];
  if (!failure || failure.code !== 'parser_failed' || failure.retryable !== true) {
    throw new Error('parser_failure_reason_invalid');
  }
  const sourceReference = safeUuid(item.source_object_id, 'parser_failure_schema_invalid');
  const revisionReference = safeUuid(item.source_revision_id, 'parser_failure_schema_invalid');
  const failureReference = safeUuid(failure.failure_id, 'parser_failure_schema_invalid');
  const retryKey = `issue561-parser-retry-${config.qualificationRunId}`;
  const retryBody = {
    expected_current_revision_id: revisionReference,
    parent_failure_id: failureReference,
    parent_processing_receipt_id: null,
    source_object_id: sourceReference,
    source_revision_id: revisionReference,
  };
  const firstRetry = await submitJson(request, config, RETRY_PATH, retryKey, retryBody);
  const secondRetry = await submitJson(request, config, RETRY_PATH, retryKey, retryBody);
  const operationReference = safeUuid(firstRetry.operation_id, 'parser_retry_schema_invalid');
  if (
    operationReference !== safeUuid(secondRetry.operation_id, 'parser_retry_schema_invalid')
    || secondRetry.replay !== true
  ) throw new Error('parser_retry_replay_not_stable');
  report.failures.parser = {
    failure_reference: failureReference,
    initial_run_reference: runId,
    retry_operation_reference: operationReference,
    retry_replay: true,
    safe_code: 'parser_failed',
    terminal_acceptance: safeAcceptance(item.acceptance, 'parser_failure_schema_invalid'),
  };
  report.scenarios.push('parser_failure_retry');
  report.ui_states.push('retryable_failure');
  return runId;
}

async function qualifyIsolation(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  runId: string,
  report: ReturnType<typeof baseReport>,
): Promise<void> {
  const foreignStatus = await request.get(`/api/v1/ingestion/runs/${runId}`, {
    headers: qualificationHeaders(config, config.workspaceBId, config.environmentBId),
  });
  if (foreignStatus.status() !== 404) throw new Error('foreign_status_existence_leak');

  const path = config.corpus.text;
  const buffer = await readFile(path);
  const denied = await request.post('/api/v1/documents/batches', {
    headers: {
      ...qualificationHeaders(config, config.workspaceAId, config.environmentBId),
      'Idempotency-Key': `issue561-denied-${config.qualificationRunId}`,
    },
    multipart: { file: { buffer, mimeType: 'text/plain', name: basename(path) } },
  });
  if (denied.status() !== 403) throw new Error('cross_environment_upload_not_denied');
  const deniedBody = await denied.text();
  if (deniedBody.includes(basename(path)) || deniedBody.includes(buffer.toString('utf8'))) {
    throw new Error('denied_upload_disclosed_content');
  }
  report.isolation.workspace_status = 404;
  report.isolation.cross_environment_upload = 403;
  report.isolation.protected_content_disclosed = false;
  report.scenarios.push('workspace_environment_isolation');
}

async function qualifySafeFailureUi(
  page: Page,
  config: IngestionQualificationConfig,
  runId: string,
  report: ReturnType<typeof baseReport>,
): Promise<void> {
  await page.goto(scoped(`/runs/${runId}`, config.workspaceAId, config.environmentAId));
  await page.getByRole('heading', { name: 'Запуск обработки' }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: 'Повторить обработку файла' }).waitFor({ state: 'visible' });
  report.ui_states.push('partial', 'retryable_failure');
}

async function qualifyTombstone(
  page: Page,
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  report: ReturnType<typeof baseReport>,
): Promise<void> {
  const target = report.format_observations.find((item) => item.format === 'text');
  if (!target) throw new Error('tombstone_target_unavailable');
  const sourceReference = safeUuid(target.source_reference, 'tombstone_target_invalid');
  const revisionReference = safeUuid(target.revision_reference, 'tombstone_target_invalid');
  const receiptReference = safeUuid(target.processing_receipt_reference, 'tombstone_target_invalid');
  const response = await submitJson(
    request,
    config,
    `/api/v1/documents/${sourceReference}${TOMBSTONE_SUFFIX}`,
    `issue561-tombstone-${config.qualificationRunId}`,
    { deletion_reason: 'source_deleted', expected_current_revision_id: revisionReference },
  );
  const operationReference = safeUuid(response.operation_id, 'tombstone_schema_invalid');
  const terminal = await waitForLifecycleOperation(request, config, operationReference);
  if (terminal.status !== 'completed' || terminal.operation_kind !== 'tombstone') {
    throw new Error('tombstone_not_completed');
  }
  const disclosure = await inspectParsedContentResponse(
    request,
    config,
    sourceReference,
    revisionReference,
    receiptReference,
  );
  if (disclosure.status !== 'content_unavailable') throw new Error('tombstone_disclosure_not_fenced');
  await page.goto(scoped(`/knowledge/documents/${sourceReference}`, config.workspaceAId, config.environmentAId));
  await page.getByText('Удалён', { exact: true }).first().waitFor({ state: 'visible' });
  report.lifecycle.tombstone = {
    disclosure_status: 'content_unavailable',
    operation_reference: operationReference,
    tombstone_revision_reference: safeUuid(terminal.accepted_revision_id, 'tombstone_schema_invalid'),
  };
  report.scenarios.push('tombstone_fences_disclosure');
  report.ui_states.push('deleted');
}

async function installRuntimeBinding(page: Page, config: IngestionQualificationConfig): Promise<void> {
  await page.addInitScript(({ accessToken, environmentAId, environmentBId, identityVersion, workspaceAId, workspaceBId }) => {
    window.__SPINE_RUNTIME__ = {
      authentication: {
        getBearerCredential: () => accessToken,
        identityVersion,
        role: 'administrator',
        status: 'authenticated',
      },
      scope: {
        defaultEnvironmentId: environmentAId,
        defaultWorkspaceId: workspaceAId,
        environments: [
          { id: environmentAId, label: 'Qualification A' },
          { id: environmentBId, label: 'Qualification B' },
        ],
        workspaces: [
          { id: workspaceAId, label: 'Workspace A' },
          { id: workspaceBId, label: 'Workspace B' },
        ],
      },
    };
  }, config);
}

async function waitForTerminal(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  runId: string,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const response = await request.get(`/api/v1/ingestion/runs/${runId}`, {
      headers: qualificationHeaders(config),
    });
    if (response.status() !== 200) throw new Error('ingestion_status_unavailable');
    const value = record(await response.json(), 'ingestion_status_schema_invalid');
    if (value.terminal === true) return value;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 750));
  }
  throw new Error('ingestion_status_timeout');
}

async function submitBatch(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  paths: string[],
  idempotencyKey: string,
): Promise<Record<string, unknown>> {
  const multipart: Record<string, { buffer: Buffer; mimeType: string; name: string }> = {};
  for (const [index, path] of paths.entries()) {
    multipart[`file-${index}`] = {
      buffer: await readFile(path),
      mimeType: mediaType(path),
      name: basename(path),
    };
  }
  const response = await request.post('/api/v1/documents/batches', {
    headers: { ...qualificationHeaders(config), 'Idempotency-Key': idempotencyKey },
    multipart,
  });
  if (response.status() !== 202) throw new Error('batch_upload_rejected');
  return record(await response.json(), 'batch_upload_schema_invalid');
}

async function submitJson(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  path: string,
  idempotencyKey: string,
  data: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const response = await request.post(path, {
    data,
    headers: { ...qualificationHeaders(config), 'Idempotency-Key': idempotencyKey },
  });
  if (response.status() !== 202) throw new Error('operator_command_rejected');
  return record(await response.json(), 'operator_command_schema_invalid');
}

async function waitForLifecycleOperation(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  operationId: string,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 2 * 60_000;
  while (Date.now() < deadline) {
    const response = await request.get(`/api/v1/documents/lifecycle/operations/${operationId}`, {
      headers: qualificationHeaders(config),
    });
    if (response.status() !== 200) throw new Error('lifecycle_status_unavailable');
    const value = record(await response.json(), 'lifecycle_status_schema_invalid');
    if (['completed', 'failed', 'conflict'].includes(String(value.status))) return value;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
  }
  throw new Error('lifecycle_status_timeout');
}

async function inspectParsedContent(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  sourceObjectId: string,
  sourceRevisionId: string,
  processingReceiptId: string,
): Promise<Record<string, unknown>> {
  const value = await inspectParsedContentResponse(
    request,
    config,
    sourceObjectId,
    sourceRevisionId,
    processingReceiptId,
  );
  if (value.status !== 'ready') {
    throw new Error('parsed_content_not_ready');
  }
  return value;
}

async function inspectParsedContentResponse(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  sourceObjectId: string,
  sourceRevisionId: string,
  processingReceiptId: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(
    `/api/v1/knowledge/documents/${sourceObjectId}/revisions/${sourceRevisionId}/parsed-content?processing_receipt_id=${processingReceiptId}&page_size=100`,
    { headers: qualificationHeaders(config) },
  );
  if (response.status() !== 200) throw new Error('parsed_content_inspection_unavailable');
  const envelope = record(await response.json(), 'parsed_content_schema_invalid');
  return record(envelope.data, 'parsed_content_schema_invalid');
}

function qualificationHeaders(
  config: IngestionQualificationConfig,
  workspaceId = config.workspaceAId,
  environmentId = config.environmentAId,
): Record<string, string> {
  return {
    Authorization: `Bearer ${config.accessToken}`,
    'X-Environment-ID': environmentId,
    'X-Workspace-ID': workspaceId,
  };
}

function baseReport(config: IngestionQualificationConfig) {
  return {
    schema_version: 'issue-561-five-format-ingestion:v1',
    ticket: 561,
    status: 'blocked',
    reason_code: 'execution_not_started' as string | null,
    required_formats: REQUIRED_FORMATS,
    required_scenarios: REQUIRED_SCENARIOS,
    formats: [] as QualificationFormat[],
    scenarios: [] as string[],
    ui_states: [] as string[],
    q16_01_compatibility: 'not_verified',
    authorization: { corpus_approval: 'pass', trusted_upload_authority: 'not_verified' },
    deployment: {
      api_release_identity: config.apiReleaseIdentity,
      authorization_generation: config.authorizationGeneration,
      commit_sha: config.commitSha,
      deployment_fingerprint: config.deploymentFingerprint,
      provider_profile_identity: config.providerProfileIdentity,
      qualification_run_id: config.qualificationRunId,
      web_release_identity: config.webReleaseIdentity,
    },
    format_observations: [] as Array<Record<string, unknown>>,
    attempted_observations: [] as Array<Record<string, unknown>>,
    lifecycle: {} as Record<string, Record<string, unknown>>,
    failures: {} as Record<string, Record<string, unknown>>,
    isolation: {} as Record<string, unknown>,
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
    operator_endpoints: { retry: RETRY_PATH, tombstone_suffix: TOMBSTONE_SUFFIX },
    blockers: [569, 570],
    unverified_scenarios: [...REQUIRED_SCENARIOS] as string[],
  };
}

async function writeReport(path: string, report: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
}

function locatorKinds(value: unknown): string[] {
  return [...new Set(arrayOfRecords(value, 'parsed_content_schema_invalid').map((item) => {
    const locator = record(item.locator, 'parsed_content_schema_invalid');
    if (typeof locator.kind !== 'string' || !locator.kind) throw new Error('parsed_content_schema_invalid');
    return locator.kind;
  }))].sort();
}

function validateLocatorEvidence(format: QualificationFormat, value: unknown): void {
  const locators = arrayOfRecords(value, 'parsed_content_schema_invalid').map((item) =>
    record(item.locator, 'parsed_content_schema_invalid'));
  if (locators.length === 0) throw new Error('stable_locator_missing');
  const expectedKind: Record<QualificationFormat, string> = {
    docx: 'ooxml_text',
    markdown: 'physical_line',
    pdf: 'page_text',
    text: 'physical_line',
    xlsx: 'xlsx_cell',
  };
  for (const locator of locators) {
    if (locator.kind !== expectedKind[format]) throw new Error('stable_locator_kind_invalid');
    if (format === 'pdf' && !positiveInteger(locator.page_number)) throw new Error('stable_locator_coordinates_invalid');
    if (format === 'xlsx' && (
      locator.schema_version !== 'locator:xlsx-cell:v1'
      || typeof locator.sheet !== 'string'
      || locator.sheet.length === 0
      || !positiveInteger(locator.row)
      || typeof locator.cell_range !== 'string'
      || !validXlsxCellRange(locator.cell_range, Number(locator.row))
    )) throw new Error('stable_locator_coordinates_invalid');
    if (format === 'docx' && !validOoxmlBlockPath(locator.block_path)) {
      throw new Error('stable_locator_coordinates_invalid');
    }
    if ((format === 'markdown' || format === 'text') && !positiveInteger(locator.line_number)) {
      throw new Error('stable_locator_coordinates_invalid');
    }
    if (format === 'markdown' || format === 'text' || format === 'pdf' || format === 'docx') {
      if (
        !nonNegativeInteger(locator.start_codepoint)
        || !nonNegativeInteger(locator.end_codepoint)
        || Number(locator.end_codepoint) < Number(locator.start_codepoint)
      ) {
        throw new Error('stable_locator_coordinates_invalid');
      }
    }
  }
}

function record(value: unknown, reason: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error(reason);
  return value as Record<string, unknown>;
}

function arrayOfRecords(value: unknown, reason: string): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) throw new Error(reason);
  return value.map((item) => record(item, reason));
}

function safeUuid(value: unknown, reason: string): string {
  if (typeof value !== 'string' || !/^[0-9a-f-]{36}$/i.test(value)) throw new Error(reason);
  return value;
}

function safeDigest(value: unknown, reason: string): string {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new Error(reason);
  return value;
}

function safeCode(value: unknown, reason: string): string {
  if (typeof value !== 'string' || !/^[a-z][a-z0-9_.:-]{0,63}$/.test(value)) throw new Error(reason);
  return value;
}

function safeAcceptance(value: unknown, reason: string): string {
  if (!['pending', 'accepted', 'rejected', 'quarantined'].includes(String(value))) {
    throw new Error(reason);
  }
  return String(value);
}

function safeIngestionStatus(value: unknown, reason: string): string {
  const statuses = [
    'non_terminal', 'succeeded', 'partial', 'failed', 'rejected', 'quarantined', 'unsupported',
    'cancelled',
  ];
  if (!statuses.includes(String(value))) throw new Error(reason);
  return String(value);
}

function safeBoolean(value: unknown, reason: string): boolean {
  if (typeof value !== 'boolean') throw new Error(reason);
  return value;
}

function mediaType(path: string): string {
  const suffix = path.toLowerCase().split('.').pop();
  const types: Record<string, string> = {
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    md: 'text/markdown',
    markdown: 'text/markdown',
    pdf: 'application/pdf',
    txt: 'text/plain',
    xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  };
  const value = suffix ? types[suffix] : undefined;
  if (!value) throw new Error('qualification_media_type_unsupported');
  return value;
}

function safeReason(error: unknown): string {
  if (error instanceof Error && /^[a-z][a-z0-9_]{0,63}$/.test(error.message)) return error.message;
  return 'ingestion_qualification_failed';
}

function failureStatus(reason: string): 'blocked' | 'fail' {
  const invariantFailures = new Set([
    'cross_environment_upload_not_denied',
    'batch_upload_rejected',
    'denied_upload_disclosed_content',
    'documents_page_unavailable',
    'duplicate_replay_not_accepted',
    'duplicate_replay_not_stable',
    'five_format_upload_incomplete',
    'five_format_upload_not_accepted',
    'five_format_upload_rejected',
    'foreign_status_existence_leak',
    'parsed_content_checksum_unavailable',
    'parsed_content_inspection_unavailable',
    'parsed_content_not_ready',
    'parser_failure_false_acceptance',
    'parser_failure_reason_invalid',
    'parser_retry_replay_not_stable',
    'operator_command_rejected',
    'stable_locator_coordinates_invalid',
    'stable_locator_kind_invalid',
    'stable_locator_missing',
    'tombstone_disclosure_not_fenced',
    'tombstone_not_completed',
    'tombstone_target_unavailable',
  ]);
  return invariantFailures.has(reason) || reason.endsWith('_schema_invalid') ? 'fail' : 'blocked';
}

function validOoxmlBlockPath(value: unknown): boolean {
  if (!Array.isArray(value) || value.length === 0) return false;
  const steps = value.map((step) => record(step, 'stable_locator_coordinates_invalid'));
  let position = 0;
  while (position < steps.length) {
    const step = steps[position]!;
    if (!nonNegativeInteger(step.index)) return false;
    if (step.kind === 'paragraph') return position === steps.length - 1;
    if (
      step.kind !== 'table'
      || steps[position + 1]?.kind !== 'row'
      || steps[position + 2]?.kind !== 'cell'
      || !nonNegativeInteger(steps[position + 1]?.index)
      || !nonNegativeInteger(steps[position + 2]?.index)
    ) return false;
    position += 3;
  }
  return false;
}

function validXlsxCellRange(value: string, row: number): boolean {
  const match = /^([A-Z]+)([1-9][0-9]*)(?::([A-Z]+)([1-9][0-9]*))?$/.exec(value);
  if (!match || Number(match[2]) !== row) return false;
  if (!match[3] || !match[4]) return true;
  const columnNumber = (column: string): number => [...column].reduce(
    (result, character) => result * 26 + character.charCodeAt(0) - 64,
    0,
  );
  return columnNumber(match[3]) >= columnNumber(match[1]!) && Number(match[4]) >= row;
}

function positiveInteger(value: unknown): boolean {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function nonNegativeInteger(value: unknown): boolean {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function scoped(path: string, workspaceId: string, environmentId: string): string {
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}workspace=${workspaceId}&environment=${environmentId}`;
}
