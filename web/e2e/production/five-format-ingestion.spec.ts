import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, dirname } from 'node:path';

import {
  readIngestionQualificationConfig,
  type IngestionQualificationConfig,
} from './ingestion-qualification-config';
import {
  createIngestionQualificationReport,
  requireCompleteIngestionQualificationReport,
  REQUIRED_FORMATS,
  type IngestionQualificationReport,
  type QualificationFormat,
} from '../../src/qualification/ingestion-qualification-report';
import {
  requireSafeCode,
  requireSafeDigest,
  requireSafeUuid,
  SAFE_CODE_PATTERN,
} from '../../src/qualification/safe-metadata';

const RETRY_PATH = '/api/v1/documents/processing/retry';
const TOMBSTONE_SUFFIX = '/tombstone';

test('test_real_browser_qualifies_five_format_canonical_ingestion', async ({ page, request }) => {
  test.setTimeout(45 * 60_000);
  const config = readIngestionQualificationConfig(process.env);
  const report = createIngestionQualificationReport({
    api_release_identity: config.apiReleaseIdentity,
    authorization_generation: config.authorizationGeneration,
    commit_sha: config.commitSha,
    deployment_fingerprint: config.deploymentFingerprint,
    provider_profile_identity: config.providerProfileIdentity,
    qualification_run_id: config.qualificationRunId,
    web_release_identity: config.webReleaseIdentity,
  });

  try {
    await verifyQ16Compatibility(config);
    report.q16_01_compatibility = 'pass';
    report.authorization = {
      corpus_approval: 'pass',
      corpus_approval_reference: config.corpusApprovalReference,
      trusted_upload_authority: 'pass',
      trusted_upload_authority_reference: config.trustedUploadAuthorityReference,
    };
    await installRuntimeBinding(page, config);
    const listResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/v1/documents',
    );
    await page.goto(scoped('/knowledge/documents', config.workspaceAId, config.environmentAId));
    if ((await listResponse).status() !== 200) throw new Error('documents_page_unavailable');

    await page.locator('input[type="file"]').setInputFiles(REQUIRED_FORMATS.map((format) => config.corpus[format]));
    await page.getByRole('region', { name: 'Проверка перед отправкой' }).waitFor({ state: 'visible' });
    const uploadResponsePromise = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/v1/documents/batches',
    );
    await page.getByRole('button', { name: 'Загрузить документы' }).click();
    await page.getByRole('button', { name: 'Остановить в браузере' }).waitFor({ state: 'visible' });
    report.ui_states.push('running');
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
    await page.goto(scoped(
      `/runs/${runId}`,
      config.workspaceAId,
      config.environmentAId,
    ));
    const renderedItems = page
      .getByRole('list', { name: 'Результаты файлов' })
      .getByRole('listitem');
    await expect(renderedItems).toHaveCount(REQUIRED_FORMATS.length);
    await expect(renderedItems.getByText('Принят', { exact: true })).toHaveCount(REQUIRED_FORMATS.length);
    await expect(renderedItems.getByText('Завершена', { exact: true })).toHaveCount(REQUIRED_FORMATS.length);
    await expect(page.getByText('Завершён успешно', { exact: true }).first()).toBeVisible();
    report.scenarios.push('five_format_upload');
    report.ui_states.push('accepted');

    await qualifyDuplicateReplay(request, config, report);
    await qualifyChangedRevision(page, request, config, report);
    const parserFailureRunId = await qualifyProcessingFailureAndRetry(
      page,
      request,
      config,
      report,
      'parser',
      'parser_retryable',
    );
    await qualifyProcessingFailureAndRetry(
      undefined,
      request,
      config,
      report,
      'original_storage',
      'original_storage_unavailable',
    );
    await qualifyPreAcceptanceFailureAndRetry(request, config, report);
    await qualifyIsolation(request, config, parserFailureRunId, report);
    await qualifyTombstone(page, request, config, report);
    requireCompleteIngestionQualificationReport(report);
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
  report: IngestionQualificationReport,
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
    logical_effect_count: 1,
    replay: true,
    result_stable: true,
    run_reference: firstRunId,
    source_reference: safeUuid(item.source_object_id, 'duplicate_replay_schema_invalid'),
    revision_reference: safeUuid(item.source_revision_id, 'duplicate_replay_schema_invalid'),
  };
  report.scenarios.push('duplicate_idempotent_replay');
}

async function qualifyChangedRevision(
  page: Page,
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  report: IngestionQualificationReport,
): Promise<void> {
  const predecessor = report.format_observations.find((item) => item.format === 'text');
  if (!predecessor) throw new Error('replacement_target_unavailable');
  const sourceReference = safeUuid(predecessor.source_reference, 'replacement_target_invalid');
  const predecessorReference = safeUuid(predecessor.revision_reference, 'replacement_target_invalid');
  await page.goto(scoped(
    `/knowledge/documents/${sourceReference}`,
    config.workspaceAId,
    config.environmentAId,
  ));
  await page.getByLabel('Файл новой ревизии').setInputFiles(config.replacementPath);
  const key = `issue561-replacement-${config.qualificationRunId}`;
  const first = await submitReplacementUpload(
    request,
    config,
    sourceReference,
    predecessorReference,
    config.replacementPath,
    key,
  );
  const replay = await submitReplacementUpload(
    request,
    config,
    sourceReference,
    predecessorReference,
    config.replacementPath,
    key,
  );
  const runReference = safeUuid(first.run_id, 'replacement_schema_invalid');
  const itemReference = safeUuid(first.item_id, 'replacement_schema_invalid');
  const newRevisionReference = safeUuid(first.source_revision_id, 'replacement_schema_invalid');
  if (
    newRevisionReference === predecessorReference
    || safeUuid(replay.run_id, 'replacement_schema_invalid') !== runReference
    || safeUuid(replay.item_id, 'replacement_schema_invalid') !== itemReference
    || safeUuid(replay.source_revision_id, 'replacement_schema_invalid') !== newRevisionReference
    || replay.replay !== true
  ) throw new Error('replacement_replay_not_stable');
  const terminal = await waitForItemTerminal(request, config, runReference, itemReference);
  if (terminal.acceptance !== 'accepted' || terminal.status !== 'succeeded') {
    throw new Error('replacement_not_accepted');
  }
  const receipt = record(terminal.processing_receipt, 'replacement_schema_invalid');
  const receiptReference = safeUuid(receipt.receipt_id, 'replacement_schema_invalid');
  const revisionsResponse = await request.get(`/api/v1/documents/${sourceReference}/revisions`, {
    headers: qualificationHeaders(config),
  });
  if (revisionsResponse.status() !== 200) throw new Error('replacement_history_unavailable');
  const revisionsEnvelope = record(await revisionsResponse.json(), 'replacement_history_schema_invalid');
  const revisions = arrayOfRecords(revisionsEnvelope.items, 'replacement_history_schema_invalid');
  const prior = revisions.find((revision) => revision.source_revision_id === predecessorReference);
  const current = revisions.find((revision) => revision.source_revision_id === newRevisionReference);
  if (
    prior?.lifecycle !== 'superseded'
    || prior.superseded_by_revision_id !== newRevisionReference
    || current?.lifecycle !== 'current'
    || current.predecessor_revision_id !== predecessorReference
  ) throw new Error('replacement_history_not_preserved');
  const conflictResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/v1/documents/${sourceReference}/replacement-upload`,
  );
  await page.getByRole('button', { name: 'Заменить документ' }).click();
  if ((await conflictResponse).status() !== 409) throw new Error('replacement_stale_command_not_rejected');
  await page.getByRole('alert', { name: 'Команда конфликтует с текущим состоянием' })
    .waitFor({ state: 'visible' });
  report.ui_states.push('stale');
  await page.goto(scoped(
    `/knowledge/documents/${sourceReference}?tab=revisions`,
    config.workspaceAId,
    config.environmentAId,
  ));
  await page.getByText('Замещён', { exact: true }).first().waitFor({ state: 'visible' });
  report.lifecycle.changed_revision = {
    new_revision_reference: newRevisionReference,
    predecessor_preserved: true,
    predecessor_revision_reference: predecessorReference,
    processing_receipt_reference: receiptReference,
    replay: true,
    result_stable: true,
    source_reference: sourceReference,
  };
}

async function qualifyProcessingFailureAndRetry(
  page: Page | undefined,
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  report: IngestionQualificationReport,
  reportKey: 'parser' | 'original_storage',
  reasonCode: 'parser_retryable' | 'original_storage_unavailable',
): Promise<string> {
  const key = `issue561-${reportKey}-failure-${config.qualificationRunId}`;
  const paths = reportKey === 'parser'
    ? [config.corpus.text, config.corpus.markdown]
    : [config.corpus.text];
  const upload = await submitBatch(request, config, paths, key);
  const runId = safeUuid(upload.run_id, 'processing_failure_schema_invalid');
  const terminal = await waitForTerminal(request, config, runId);
  const items = arrayOfRecords(terminal.items, 'processing_failure_schema_invalid');
  const item = items.find((candidate) => candidate.input_ordinal === 1);
  if (reportKey === 'parser') {
    const unaffected = items.find((candidate) => candidate.input_ordinal === 2);
    if (terminal.status !== 'partial' || unaffected?.status !== 'succeeded') {
      throw new Error('partial_failure_not_visible');
    }
  }
  if (item?.status === 'succeeded') throw new Error(`${reportKey}_failure_not_injected`);
  if (!item || item.acceptance !== 'accepted' || item.retryable !== true || item.status !== 'failed') {
    throw new Error('processing_failure_state_invalid');
  }
  const failure = arrayOfRecords(item.failures, 'processing_failure_schema_invalid')
    .find((candidate) => candidate.code === reasonCode);
  if (!failure || failure.retryable !== true) throw new Error('processing_failure_reason_invalid');
  if (page) await qualifySafeFailureUi(page, config, runId, report);
  const sourceReference = safeUuid(item.source_object_id, 'processing_failure_schema_invalid');
  const revisionReference = safeUuid(item.source_revision_id, 'processing_failure_schema_invalid');
  const failureReference = safeUuid(failure.failure_id, 'processing_failure_schema_invalid');
  const processingReceipt = item.processing_receipt === null || item.processing_receipt === undefined
    ? null
    : record(item.processing_receipt, 'processing_failure_schema_invalid');
  const retryKey = `issue561-${reportKey}-retry-${config.qualificationRunId}`;
  const retryBody = {
    expected_current_revision_id: revisionReference,
    parent_failure_id: failureReference,
    parent_processing_receipt_id: processingReceipt
      ? safeUuid(processingReceipt.receipt_id, 'processing_failure_schema_invalid')
      : null,
    source_object_id: sourceReference,
    source_revision_id: revisionReference,
  };
  const firstRetry = await submitJson(request, config, RETRY_PATH, retryKey, retryBody);
  const secondRetry = await submitJson(request, config, RETRY_PATH, retryKey, retryBody);
  const operationReference = safeUuid(firstRetry.operation_id, 'processing_retry_schema_invalid');
  if (
    operationReference !== safeUuid(secondRetry.operation_id, 'processing_retry_schema_invalid')
    || secondRetry.replay !== true
  ) throw new Error('processing_retry_replay_not_stable');
  const retryTerminal = await waitForProcessingOperation(request, config, operationReference);
  if (retryTerminal.status !== 'accepted') {
    throw new Error('processing_retry_not_ready');
  }
  safeUuid(retryTerminal.processing_receipt_id, 'processing_retry_schema_invalid');
  const retryItem = await waitForItemTerminal(
    request,
    config,
    safeUuid(retryTerminal.run_id, 'processing_retry_schema_invalid'),
    safeUuid(retryTerminal.item_id, 'processing_retry_schema_invalid'),
  );
  if (retryItem.status !== 'succeeded' || retryItem.acceptance !== 'accepted') {
    throw new Error('processing_retry_not_ready');
  }
  report.failures[reportKey] = {
    accepted_before_retry: item.acceptance === 'accepted',
    failure_reference: failureReference,
    initial_run_reference: runId,
    logical_effect_count: 1,
    reason_code: reasonCode,
    retry_operation_reference: operationReference,
    retry_passed: true,
    retry_replay: true,
  };
  report.ui_states.push('retryable_failure');
  return runId;
}

async function qualifyPreAcceptanceFailureAndRetry(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  report: IngestionQualificationReport,
): Promise<void> {
  const key = `issue561-pre-acceptance-${config.qualificationRunId}`;
  const first = await submitSingleUpload(request, config, config.corpus.text, key);
  const runReference = safeUuid(first.run_id, 'pre_acceptance_schema_invalid');
  const itemReference = safeUuid(first.item_id, 'pre_acceptance_schema_invalid');
  const failed = await waitForItemTerminal(request, config, runReference, itemReference);
  const failureCodes = arrayOfSafeCodes(failed.failure_reason_codes, 'pre_acceptance_schema_invalid');
  const failureIds = arrayOfSafeUuids(failed.failure_ids, 'pre_acceptance_schema_invalid');
  if (failed.status === 'succeeded') throw new Error('pre_acceptance_failure_not_injected');
  if (
    failed.acceptance === 'accepted'
    || failed.status !== 'failed'
    || !failureCodes.includes('pre_acceptance_retryable')
    || failureIds.length !== 1
  ) throw new Error('pre_acceptance_failure_state_invalid');
  const retry = await submitSingleUpload(request, config, config.corpus.text, key);
  const replay = await submitSingleUpload(request, config, config.corpus.text, key);
  if (
    retry.replay !== true
    || replay.replay !== true
    || safeUuid(retry.run_id, 'pre_acceptance_schema_invalid') !== runReference
    || safeUuid(replay.run_id, 'pre_acceptance_schema_invalid') !== runReference
    || safeUuid(retry.item_id, 'pre_acceptance_schema_invalid') !== itemReference
    || safeUuid(replay.item_id, 'pre_acceptance_schema_invalid') !== itemReference
  ) throw new Error('pre_acceptance_retry_replay_not_stable');
  const succeeded = await waitForItemTerminal(request, config, runReference, itemReference);
  if (succeeded.acceptance !== 'accepted' || succeeded.status !== 'succeeded') {
    throw new Error('pre_acceptance_retry_not_accepted');
  }
  report.failures.post_write_pre_acceptance = {
    accepted_before_retry: false,
    failure_reference: failureIds[0]!,
    initial_run_reference: runReference,
    logical_effect_count: 1,
    reason_code: 'pre_acceptance_retryable',
    retry_passed: true,
    retry_replay: true,
  };
}

async function qualifyIsolation(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  runId: string,
  report: IngestionQualificationReport,
): Promise<void> {
  const path = config.corpus.text;
  const buffer = await readFile(path);
  const deniedScopes = [
    {
      dimension: 'workspace' as const,
      environmentId: config.environmentAId,
      workspaceId: config.workspaceBId,
    },
    {
      dimension: 'environment' as const,
      environmentId: config.environmentBId,
      workspaceId: config.workspaceAId,
    },
  ];
  for (const scope of deniedScopes) {
    const statusResponse = await request.get(`/api/v1/ingestion/runs/${runId}`, {
      headers: qualificationHeaders(config, scope.workspaceId, scope.environmentId),
    });
    if (![403, 404].includes(statusResponse.status())) throw new Error('foreign_status_existence_leak');
    await assertDeniedResponseIsSafe(statusResponse, path, buffer, 'spine.authorization.denied');

    const uploadResponse = await request.post('/api/v1/documents/batches', {
      headers: {
        ...qualificationHeaders(config, scope.workspaceId, scope.environmentId),
        'Idempotency-Key': `issue561-denied-${scope.dimension}-${config.qualificationRunId}`,
      },
      multipart: { file: { buffer, mimeType: 'text/plain', name: basename(path) } },
    });
    if (uploadResponse.status() !== 403) throw new Error(`cross_${scope.dimension}_upload_not_denied`);
    await assertDeniedResponseIsSafe(uploadResponse, path, buffer, 'spine.authorization.denied');
    if (scope.dimension === 'workspace') {
      report.isolation.denied_workspace_status_read = 'pass';
      report.isolation.denied_workspace_upload = 'pass';
    } else {
      report.isolation.denied_environment_status_read = 'pass';
      report.isolation.denied_environment_upload = 'pass';
    }
  }
  report.isolation.protected_metadata_disclosed = false;
}

async function qualifySafeFailureUi(
  page: Page,
  config: IngestionQualificationConfig,
  runId: string,
  report: IngestionQualificationReport,
): Promise<void> {
  await page.goto(scoped(`/runs/${runId}`, config.workspaceAId, config.environmentAId));
  await page
    .getByRole('heading', { name: 'Запуск', exact: true })
    .waitFor({ state: 'visible', timeout: 30_000 });
  await page.getByText('parser_retryable').first().waitFor({ state: 'visible', timeout: 30_000 });
  await page
    .getByRole('button', { name: 'Повторить обработку файла' })
    .waitFor({ state: 'visible', timeout: 30_000 });
  report.ui_states.push('partial', 'retryable_failure');
}

async function qualifyTombstone(
  page: Page,
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  report: IngestionQualificationReport,
): Promise<void> {
  const target = report.lifecycle.changed_revision;
  if (!target) throw new Error('tombstone_target_unavailable');
  const sourceReference = safeUuid(target.source_reference, 'tombstone_target_invalid');
  const revisionReference = safeUuid(target.new_revision_reference, 'tombstone_target_invalid');
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
  const revisionsResponse = await request.get(`/api/v1/documents/${sourceReference}/revisions`, {
    headers: qualificationHeaders(config),
  });
  if (revisionsResponse.status() !== 200) throw new Error('tombstone_history_unavailable');
  const revisionsEnvelope = record(await revisionsResponse.json(), 'tombstone_history_schema_invalid');
  const revisions = arrayOfRecords(revisionsEnvelope.items, 'tombstone_history_schema_invalid');
  const retainedRevision = revisions.find((revision) => revision.source_revision_id === revisionReference);
  const tombstoneReference = safeUuid(terminal.accepted_revision_id, 'tombstone_schema_invalid');
  const tombstoneRevision = revisions.find((revision) => revision.source_revision_id === tombstoneReference);
  if (!retainedRevision || tombstoneRevision?.lifecycle !== 'tombstoned') {
    throw new Error('tombstone_history_not_preserved');
  }
  await page.goto(scoped(`/knowledge/documents/${sourceReference}`, config.workspaceAId, config.environmentAId));
  await page.getByText('Удалён', { exact: true }).first().waitFor({ state: 'visible' });
  report.lifecycle.tombstone = {
    disclosure_fenced: true,
    history_preserved: true,
    operation_reference: operationReference,
    tombstone_revision_reference: tombstoneReference,
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

async function submitReplacementUpload(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  sourceObjectId: string,
  expectedRevisionId: string,
  path: string,
  idempotencyKey: string,
): Promise<Record<string, unknown>> {
  const response = await request.post(`/api/v1/documents/${sourceObjectId}/replacement-upload`, {
    data: await readFile(path),
    headers: {
      ...qualificationHeaders(config),
      'Content-Type': mediaType(path),
      'Idempotency-Key': idempotencyKey,
      'If-Match': expectedRevisionId,
      'X-Filename': basename(path),
    },
  });
  if (response.status() !== 202) throw new Error('replacement_upload_rejected');
  return record(await response.json(), 'replacement_schema_invalid');
}

async function submitSingleUpload(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  path: string,
  idempotencyKey: string,
): Promise<Record<string, unknown>> {
  const response = await request.post('/api/v1/documents/uploads', {
    data: await readFile(path),
    headers: {
      ...qualificationHeaders(config),
      'Content-Type': mediaType(path),
      'Idempotency-Key': idempotencyKey,
      'X-Filename': basename(path),
    },
  });
  if (response.status() !== 202) throw new Error('single_upload_rejected');
  return record(await response.json(), 'single_upload_schema_invalid');
}

async function waitForItemTerminal(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  runId: string,
  itemId: string,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const response = await request.get(`/api/v1/ingestion/runs/${runId}/items/${itemId}`, {
      headers: qualificationHeaders(config),
    });
    if (response.status() !== 200) throw new Error('ingestion_item_status_unavailable');
    const value = record(await response.json(), 'ingestion_item_status_schema_invalid');
    if (value.terminal === true) return value;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 750));
  }
  throw new Error('ingestion_item_status_timeout');
}

async function waitForProcessingOperation(
  request: APIRequestContext,
  config: IngestionQualificationConfig,
  operationId: string,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const response = await request.get(`/api/v1/documents/processing/operations/${operationId}`, {
      headers: qualificationHeaders(config),
    });
    if (response.status() !== 200) throw new Error('processing_operation_unavailable');
    const value = record(await response.json(), 'processing_operation_schema_invalid');
    if (
      (value.status === 'accepted' && value.processing_receipt_id !== null)
      || ['partial', 'failed', 'cancelled', 'unavailable'].includes(String(value.status))
    ) return value;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
  }
  throw new Error('processing_operation_timeout');
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

async function writeReport(path: string, report: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
}

async function assertDeniedResponseIsSafe(
  response: Awaited<ReturnType<APIRequestContext['get']>>,
  path: string,
  content: Buffer,
  expectedCode: 'spine.authorization.denied',
): Promise<void> {
  const body = await response.text();
  const lower = body.toLowerCase();
  let envelope: Record<string, unknown>;
  try {
    envelope = record(JSON.parse(body), 'denied_response_schema_invalid');
  } catch {
    throw new Error('denied_response_schema_invalid');
  }
  if (safeCode(envelope.code, 'denied_response_schema_invalid') !== expectedCode) {
    throw new Error('denied_response_reason_invalid');
  }
  if (envelope.safe_message !== 'Authorization is denied.') {
    throw new Error('denied_response_schema_invalid');
  }
  if (
    body.includes(basename(path))
    || body.includes(content.toString('utf8'))
    || lower.includes('traceback')
    || lower.includes('stack trace')
    || lower.includes('exception:')
  ) throw new Error('denied_response_disclosed_protected_data');
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
  return requireSafeUuid(value, reason);
}

function safeDigest(value: unknown, reason: string): string {
  return requireSafeDigest(value, reason);
}

function safeCode(value: unknown, reason: string): string {
  return requireSafeCode(value, reason);
}

function arrayOfSafeCodes(value: unknown, reason: string): string[] {
  if (!Array.isArray(value)) throw new Error(reason);
  return value.map((item) => safeCode(item, reason));
}

function arrayOfSafeUuids(value: unknown, reason: string): string[] {
  if (!Array.isArray(value)) throw new Error(reason);
  return value.map((item) => safeUuid(item, reason));
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
  if (error instanceof Error && SAFE_CODE_PATTERN.test(error.message)) return error.message;
  return 'ingestion_qualification_failed';
}

function failureStatus(reason: string): 'blocked' | 'fail' | 'not_verified' {
  if ([
    'original_storage_failure_not_injected',
    'parser_failure_not_injected',
    'pre_acceptance_failure_not_injected',
    'remaining_lifecycle_edges_not_exercised',
  ].includes(reason)) return 'not_verified';
  const invariantFailures = new Set([
    'cross_workspace_upload_not_denied',
    'cross_environment_upload_not_denied',
    'batch_upload_rejected',
    'denied_response_disclosed_protected_data',
    'denied_response_reason_invalid',
    'documents_page_unavailable',
    'duplicate_replay_not_accepted',
    'duplicate_replay_not_stable',
    'five_format_upload_incomplete',
    'five_format_upload_not_accepted',
    'five_format_upload_rejected',
    'five_format_ui_not_accepted',
    'foreign_status_existence_leak',
    'ingestion_item_status_unavailable',
    'partial_failure_not_visible',
    'parsed_content_checksum_unavailable',
    'parsed_content_inspection_unavailable',
    'parsed_content_not_ready',
    'pre_acceptance_failure_state_invalid',
    'pre_acceptance_retry_not_accepted',
    'pre_acceptance_retry_replay_not_stable',
    'processing_failure_reason_invalid',
    'processing_failure_state_invalid',
    'processing_operation_unavailable',
    'processing_retry_not_ready',
    'processing_retry_replay_not_stable',
    'operator_command_rejected',
    'replacement_history_not_preserved',
    'replacement_history_unavailable',
    'replacement_not_accepted',
    'replacement_replay_not_stable',
    'replacement_stale_command_not_rejected',
    'replacement_target_unavailable',
    'replacement_upload_rejected',
    'single_upload_rejected',
    'stable_locator_coordinates_invalid',
    'stable_locator_kind_invalid',
    'stable_locator_missing',
    'tombstone_disclosure_not_fenced',
    'tombstone_history_not_preserved',
    'tombstone_history_unavailable',
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
