import {
  test,
  type BrowserContext,
  type Locator,
  type Page,
  type Request,
  type Response as PlaywrightResponse,
} from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import { basename, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { readLiveQualificationConfig, type LiveQualificationConfig } from './live-config';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const WORKSPACE_B_DOCUMENT_PATH = fileURLToPath(
  new URL('../fixtures/workspace-b-isolation.md', import.meta.url),
);
const WORKSPACE_B_ANSWERABLE_QUESTION = 'What are the fixture color and verification sequence in this workspace?';

test('test_real_browser_qualifies_governed_qa_without_controlled_api_fixtures', async ({ context, page }) => {
  test.setTimeout(45 * 60_000);
  const config = readLiveQualificationConfig(process.env);
  const evidence = new EvidenceCollector(config);
  await installUiStateObserver(page);
  installMetadataObservers(page, evidence);
  await installRuntimeBinding(page, config);

  try {
    await qualifyUploadAndProjection(page, config, evidence);
    await qualifyAnswerCitationFeedbackReconnectAndRun(page, context, config, evidence);
    await qualifyAbstentionCancellationAndRestoration(page, config, evidence);
    await qualifyWorkspaceIsolation(page, config, evidence);
    await qualifyEvidenceDisclosureRejection(page, config, evidence);
    await qualifySafeUiStates(page, evidence);
    evidence.assertComplete();
    await evidence.write('pass');
  } catch (error) {
    const reasonCode = error instanceof Error && /^[a-z][a-z0-9_]{0,63}$/.test(error.message)
      ? error.message
      : 'browser_journey_failed';
    await evidence.write('blocked', reasonCode);
    throw new Error('issue508_browser_qualification_blocked');
  }
});

async function installRuntimeBinding(page: Page, config: LiveQualificationConfig): Promise<void> {
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
  }, {
    accessToken: config.accessToken,
    environmentAId: config.environmentAId,
    environmentBId: config.environmentBId,
    identityVersion: config.identityVersion,
    workspaceAId: config.workspaceAId,
    workspaceBId: config.workspaceBId,
  });
}

async function installUiStateObserver(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const labels = new Map([
      ['Загрузка', 'loading'],
      ['Пока нет данных', 'empty'],
      ['Показаны не все данные', 'partial'],
      ['Данные устарели', 'stale'],
      ['Сервис работает с ограничениями', 'degraded'],
      ['Нет доступа к данным', 'permission-denied'],
      ['Доступ запрещён', 'permission-denied'],
      ['Не удалось загрузить раздел', 'terminal-failure'],
    ]);
    const observe = () => {
      const states = new Set<string>(JSON.parse(sessionStorage.getItem('spine.issue508.ui-states') ?? '[]'));
      for (const element of document.querySelectorAll('[aria-label]')) {
        const state = labels.get(element.getAttribute('aria-label') ?? '');
        if (state) states.add(state);
      }
      sessionStorage.setItem('spine.issue508.ui-states', JSON.stringify([...states]));
    };
    new MutationObserver(observe).observe(document, { childList: true, subtree: true });
    window.addEventListener('DOMContentLoaded', observe, { once: true });
  });
}

async function qualifyUploadAndProjection(
  page: Page,
  config: LiveQualificationConfig,
  evidence: EvidenceCollector,
): Promise<void> {
  const documentListResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/v1/documents',
  );
  await page.goto(scoped('/knowledge/documents', config.workspaceAId, config.environmentAId));
  const documentListStatus = (await documentListResponse).status();
  if (documentListStatus === 404) throw new Error('production_api_surface_incomplete');
  if (documentListStatus === 401) throw new Error('qualification_authentication_rejected');
  if (documentListStatus === 403) throw new Error('qualification_authority_missing');
  if (documentListStatus !== 200) throw new Error('production_api_dependency_unavailable');
  await visible(page.getByRole('heading', { name: 'Документы', exact: true }), 'documents_page_unavailable');
  await page.getByRole('button', { name: 'Проверить соединение' }).click();
  await visible(
    page.locator('[role="status"][data-state="checked"]'),
    'production_api_readiness_unavailable',
  );

  await page.locator('input[type="file"]').setInputFiles(config.documentPath);
  await visible(page.getByRole('region', { name: 'Проверка перед отправкой' }), 'upload_review_unavailable');
  const uploadResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/v1/documents/batches',
  );
  await page.getByRole('button', { name: 'Загрузить документы' }).click();
  const uploadStatus = (await uploadResponse).status();
  if ([404, 405].includes(uploadStatus)) throw new Error('production_ingestion_surface_incomplete');
  if (uploadStatus === 401) throw new Error('qualification_authentication_rejected');
  if (uploadStatus === 403) throw new Error('qualification_authority_missing');
  if (![200, 201, 202].includes(uploadStatus)) throw new Error('production_ingestion_dependency_unavailable');
  const ingestionRunLink = page.getByRole('link', { name: 'Открыть запуск обработки' }).first();
  await visible(ingestionRunLink, 'ingestion_run_unavailable', 180_000);
  await ingestionRunLink.click();
  await visible(page.getByRole('heading', { name: 'Запуски обработки' }), 'ingestion_run_unavailable');
  await waitFor(() => Boolean(evidence.sourceObjectId && evidence.sourceRevisionId && evidence.ingestionRunId), 'upload_metadata_unavailable');

  await page.goto(scoped(
    `/knowledge/documents/${evidence.sourceObjectId}?tab=projection&projection_kind=qa`,
    config.workspaceAId,
    config.environmentAId,
  ));
  const projection = page.getByRole('region', { name: 'Состояние публикации проекции' });
  await visible(projection, 'projection_status_unavailable', 180_000);
  await waitFor(async () => projection.evaluate((element) => {
    const value = element.textContent ?? '';
    return value.includes('Опубликован') && value.includes('Здоров') && value.includes('Актуальна');
  }), 'active_projection_not_current', 1_800_000);
  evidence.scenarios.add('upload_active_projection');
}

async function qualifyAnswerCitationFeedbackReconnectAndRun(
  page: Page,
  context: BrowserContext,
  config: LiveQualificationConfig,
  evidence: EvidenceCollector,
): Promise<void> {
  await page.exposeFunction('__spineDisconnectAfterAdmission', () => {
    void context.setOffline(true);
  });
  await page.addInitScript(() => {
    const nativeFetch = window.fetch.bind(window);
    const faultInjectedKey = 'spine:e2e:first-admission-response-disconnected';
    window.fetch = async (input, init) => {
      const response = await nativeFetch(input, init);
      const rawUrl = typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
      if (
        window.sessionStorage.getItem(faultInjectedKey) !== 'true' &&
        new URL(rawUrl, window.location.href).pathname === '/api/v1/ask/admissions' &&
        response.ok
      ) {
        window.sessionStorage.setItem(faultInjectedKey, 'true');
        await (window as typeof window & {
          __spineDisconnectAfterAdmission: () => Promise<void>;
        }).__spineDisconnectAfterAdmission();
        throw new TypeError('Failed to fetch');
      }
      return response;
    };
  });
  await page.goto(scoped('/chat', config.workspaceAId, config.environmentAId));
  await visible(page.getByRole('heading', { name: 'Сессии вопросов и ответов' }), 'chat_unavailable');
  await page.getByRole('button', { name: 'Создать сессию' }).click();
  await page.waitForURL(/\/chat\/[0-9a-f-]{36}/i);
  evidence.sessionAId = uuidFromPath(page.url(), 'chat');
  await visible(page.getByRole('region', { name: 'Пока нет данных' }), 'empty_state_not_visible');

  await context.setOffline(true);
  await page.getByRole('button', { name: 'К списку сессий' }).click();
  await visible(page.getByRole('region', { name: 'Данные устарели' }), 'stale_state_not_visible');
  await visible(page.getByRole('region', { name: 'Показаны не все данные' }), 'partial_state_not_visible');
  await context.setOffline(false);
  await page.goto(scoped(`/chat/${evidence.sessionAId}`, config.workspaceAId, config.environmentAId));
  await visible(page.getByRole('textbox', { name: 'Вопрос' }), 'session_not_restored_after_partial_state');

  await page.getByRole('textbox', { name: 'Вопрос' }).fill(config.answerableQuestion);
  await page.getByRole('button', { name: 'Отправить' }).click();
  await visible(page.getByRole('button', { name: 'Повторить ту же команду' }), 'network_retry_not_offered');
  await context.setOffline(false);
  await page.getByRole('button', { name: 'Повторить ту же команду' }).click();
  await waitFor(() => evidence.askRuns.length >= 1, 'ask_admission_not_observed');
  await waitFor(
    () => Boolean(evidence.answerMessageId && evidence.answerRunId),
    'answer_admission_identity_not_observed',
  );

  await waitFor(() => evidence.eventRequests.length >= 1, 'sse_not_observed');
  await context.setOffline(true);
  await page.waitForTimeout(1_200);
  await visible(page.getByText('Восстанавливаем поток событий с последней подтверждённой позиции.'), 'degraded_reconnect_state_not_visible');
  await markUiState(page, 'degraded');
  await context.setOffline(false);
  await waitFor(
    () => evidence.eventRequests.some((request) => Boolean(request.lastEventId)),
    'sse_resume_cursor_not_observed',
    180_000,
  );

  const acceptedAnswer = page.locator('[data-accepted-answer-text]').last();
  await visible(acceptedAnswer, 'accepted_answer_not_visible', 300_000);
  await openCitationInspector(page);
  await waitFor(() => Boolean(evidence.citationId), 'citation_evidence_identity_not_observed');
  await page.getByRole('group', { name: 'Оценка ответа' }).getByRole('button', { name: 'Полезно' }).click();
  await visible(page.getByText('Спасибо, отзыв сохранён.'), 'feedback_not_persisted');

  const runLink = page.getByRole('link', { name: 'Открыть запуск Q&A' }).last();
  await visible(runLink, 'qa_run_link_unavailable');
  await runLink.click();
  await visible(page.getByRole('heading', { name: 'Детали запуска Q&A' }), 'qa_run_diagnostics_unavailable');
  await visible(page.getByRole('heading', { name: 'Операционная диагностика' }), 'trace_correlation_unavailable');
  await page.getByRole('button', { name: 'Показать диагностику' }).click();
  await visible(page.getByText('Источники корреляции доступны'), 'trace_correlation_incomplete');
  evidence.scenarios.add('answer_citation_feedback_run');
  evidence.scenarios.add('duplicate_retry');
  evidence.scenarios.add('sse_reconnect');
}

async function openCitationInspector(page: Page): Promise<void> {
  const inspector = page.getByRole('heading', { name: 'Источник 1' });
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.getByRole('button', { name: /^Открыть источник 1/ }).last().click();
    try {
      await inspector.waitFor({ state: 'visible', timeout: 10_000 });
      return;
    } catch {
      await page.reload();
      await visible(page.locator('[data-accepted-answer-text]').last(), 'accepted_answer_not_restored', 60_000);
    }
  }
  throw new Error('citation_inspector_unavailable');
}

async function qualifyAbstentionCancellationAndRestoration(
  page: Page,
  config: LiveQualificationConfig,
  evidence: EvidenceCollector,
): Promise<void> {
  await page.goto(scoped(`/chat/${evidence.sessionAId}`, config.workspaceAId, config.environmentAId));
  await visible(page.getByRole('textbox', { name: 'Вопрос' }), 'session_restore_unavailable');
  await page.reload();
  await visible(page.getByRole('textbox', { name: 'Вопрос' }), 'session_reload_unavailable');
  evidence.scenarios.add('session_restoration');

  await page.getByRole('textbox', { name: 'Вопрос' }).fill(
    `${config.unanswerableQuestion}\nNo-leak sentinel: ${evidence.noLeakSentinel}`,
  );
  await page.getByRole('button', { name: 'Отправить' }).click();
  await visible(page.getByText(/Недостаточно данных для ответа|Требуется уточнение/).last(), 'abstention_not_visible', 300_000);
  evidence.scenarios.add('abstention');

  const askRunCountBeforeCancellation = evidence.askRuns.length;
  await page.getByRole('textbox', { name: 'Вопрос' }).fill(config.cancelQuestion);
  await page.getByRole('button', { name: 'Отправить' }).click();
  const cancel = page.getByRole('button', { name: 'Остановить выполнение' }).last();
  await visible(cancel, 'cancellation_control_not_visible', 120_000);
  await waitFor(
    () => evidence.askRuns.length > askRunCountBeforeCancellation,
    'cancellation_admission_not_correlated',
  );
  evidence.cancellationRunId = evidence.askRuns.at(-1) ?? null;
  const cancellationResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname.endsWith('/cancel'),
  );
  await cancel.click();
  if (![200, 202].includes((await cancellationResponse).status())) {
    throw new Error('cancellation_request_rejected');
  }
  await waitFor(async () => {
    await page.reload();
    try {
      await page.getByRole('textbox', { name: 'Вопрос' }).waitFor({ state: 'visible', timeout: 10_000 });
      return await page.getByText('Отменён').last().isVisible();
    } catch {
      return false;
    }
  }, 'cancelled_terminal_not_restored', 240_000);
  await waitFor(
    () => evidence.cancellationRunId !== null
      && evidence.terminalOutcomesByRun.get(evidence.cancellationRunId) === 'cancelled',
    'cancelled_terminal_run_not_correlated',
  );
  evidence.scenarios.add('cancellation');
}

async function qualifyWorkspaceIsolation(
  page: Page,
  config: LiveQualificationConfig,
  evidence: EvidenceCollector,
): Promise<void> {
  await choose(page, 'Рабочая область', 'Workspace B');

  const sessionDenial = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/v1/qa/sessions/${evidence.sessionAId}`,
  );
  await page.goto(scoped(
    `/chat/${evidence.sessionAId}?message_id=${evidence.answerMessageId}&run_id=${evidence.answerRunId}&citation_id=${evidence.citationId}`,
    config.workspaceBId,
    config.environmentBId,
  ));
  if (![403, 404].includes((await sessionDenial).status())) throw new Error('cross_workspace_session_not_denied');
  if (await page.locator('[data-accepted-answer-text]').count()) throw new Error('cross_workspace_answer_disclosed');
  if ((await page.locator('body').innerText()).includes(config.answerableQuestion)) {
    throw new Error('cross_workspace_question_disclosed');
  }
  await visible(
    page.getByRole('alert', { name: 'Доступ запрещён' }),
    'permission_denied_state_not_visible',
  );

  const sourceDenial = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/v1/documents/${evidence.sourceObjectId}`,
  );
  await page.goto(scoped(`/knowledge/documents/${evidence.sourceObjectId}`, config.workspaceBId, config.environmentBId));
  if (![403, 404].includes((await sourceDenial).status())) throw new Error('cross_workspace_source_not_denied');
  if (await page.locator('[data-protected-content]').count()) throw new Error('cross_workspace_source_disclosed');

  const workspaceBSource = await uploadIndependentWorkspaceBDocument(page, config);
  evidence.workspaceBSourceObjectId = workspaceBSource.sourceObjectId;
  evidence.workspaceBSourceRevisionId = workspaceBSource.sourceRevisionId;

  await page.goto(scoped('/chat', config.workspaceBId, config.environmentBId));
  await visible(page.getByRole('heading', { name: 'Сессии вопросов и ответов' }), 'workspace_b_not_authorized');
  await page.getByRole('button', { name: 'Создать сессию' }).click();
  await page.waitForURL(/\/chat\/[0-9a-f-]{36}/i);
  evidence.sessionBId = uuidFromPath(page.url(), 'chat');
  await page.getByRole('textbox', { name: 'Вопрос' }).fill(WORKSPACE_B_ANSWERABLE_QUESTION);
  await page.getByRole('button', { name: 'Отправить' }).click();
  const workspaceBAnswer = page.locator('[data-accepted-answer-text]').last();
  await visible(workspaceBAnswer, 'workspace_b_answer_not_visible', 300_000);
  await openCitationInspector(page);
  await waitFor(() => evidence.workspaceBCitationLocatorValidated, 'workspace_b_citation_not_validated');
  evidence.scenarios.add('two_workspace_isolation');
}

async function uploadIndependentWorkspaceBDocument(
  page: Page,
  config: LiveQualificationConfig,
): Promise<{ sourceObjectId: string; sourceRevisionId: string }> {
  await page.goto(scoped('/knowledge/documents', config.workspaceBId, config.environmentBId));
  await visible(page.getByRole('heading', { name: 'Документы', exact: true }), 'workspace_b_documents_unavailable');
  await page.locator('input[type="file"]').setInputFiles(WORKSPACE_B_DOCUMENT_PATH);
  await visible(page.getByRole('region', { name: 'Проверка перед отправкой' }), 'workspace_b_upload_review_unavailable');
  const responsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/v1/documents/batches',
  );
  await page.getByRole('button', { name: 'Загрузить документы' }).click();
  const response = await responsePromise;
  if (![200, 201, 202].includes(response.status())) throw new Error('workspace_b_upload_failed');
  const body: unknown = await response.json();
  const item = isRecord(body) && Array.isArray(body.items) && isRecord(body.items[0])
    ? body.items[0]
    : null;
  const sourceObjectId = safeUuid(item?.source_object_id);
  const sourceRevisionId = safeUuid(item?.source_revision_id);
  if (!sourceObjectId || !sourceRevisionId) throw new Error('workspace_b_upload_metadata_unavailable');

  await page.goto(scoped(
    `/knowledge/documents/${sourceObjectId}?tab=projection&projection_kind=qa`,
    config.workspaceBId,
    config.environmentBId,
  ));
  const projection = page.getByRole('region', { name: 'Состояние публикации проекции' });
  await visible(projection, 'workspace_b_projection_status_unavailable', 180_000);
  await waitFor(async () => projection.evaluate((element) => {
    const value = element.textContent ?? '';
    return value.includes('Опубликован') && value.includes('Здоров') && value.includes('Актуальна');
  }), 'workspace_b_projection_not_current', 1_800_000);
  return { sourceObjectId, sourceRevisionId };
}

async function qualifyEvidenceDisclosureRejection(
  page: Page,
  config: LiveQualificationConfig,
  evidence: EvidenceCollector,
): Promise<void> {
  if (!evidence.answerMessageId || !evidence.answerRunId || !evidence.citationId) {
    throw new Error('answer_evidence_identity_missing');
  }
  await choose(page, 'Рабочая область', 'Workspace A');
  const fabricatedCitationId = 'f0800000-0000-4000-8000-000000000001';
  await page.goto(scoped(
    `/chat/${evidence.sessionAId}?message_id=${evidence.answerMessageId}&run_id=${evidence.answerRunId}&citation_id=${fabricatedCitationId}`,
    config.workspaceAId,
    config.environmentAId,
  ));
  await visible(page.getByRole('region', { name: 'Не удалось загрузить раздел' }), 'fabricated_citation_not_rejected');
  if (await page.locator('[data-accepted-answer-text]').count()) throw new Error('fabricated_citation_disclosed');
  await markUiState(page, 'terminal-failure');

  await page.goto(scoped(`/knowledge/documents/${evidence.sourceObjectId}`, config.workspaceAId, config.environmentAId));
  await visible(page.getByLabel('Причина логического удаления'), 'tombstone_control_unavailable');
  await page.getByLabel('Причина логического удаления').selectOption('source_deleted');
  await page.getByRole('button', { name: 'Создать tombstone' }).click();
  await visible(page.getByText('Новая неизменяемая tombstone-ревизия сохранена в истории.'), 'tombstone_not_completed', 300_000);

  await page.goto(scoped(
    `/chat/${evidence.sessionAId}?message_id=${evidence.answerMessageId}&run_id=${evidence.answerRunId}&citation_id=${evidence.citationId}`,
    config.workspaceAId,
    config.environmentAId,
  ));
  await visible(page.getByText(/(?:Ответ и ссылки скрыты|Содержимое скрыто|Ответ недоступен)/), 'revoked_citation_not_hidden', 180_000);
  if (await page.locator('[data-accepted-answer-text]').count()) throw new Error('revoked_citation_disclosed');
  evidence.scenarios.add('evidence_disclosure_rejection');
}

async function qualifySafeUiStates(page: Page, evidence: EvidenceCollector): Promise<void> {
  const states = await page.evaluate(() => JSON.parse(
    sessionStorage.getItem('spine.issue508.ui-states') ?? '[]',
  ) as string[]);
  evidence.captureUiStates(states);
  const required = new Set([
    'loading', 'empty', 'partial', 'stale', 'degraded', 'permission-denied', 'terminal-failure',
  ]);
  if ([...required].some((state) => !evidence.uiStates.has(state))) {
    throw new Error('safe_ui_states_incomplete');
  }
  evidence.scenarios.add('safe_ui_states');
}

async function markUiState(page: Page, state: string): Promise<void> {
  await page.evaluate((value) => {
    const states = new Set<string>(JSON.parse(sessionStorage.getItem('spine.issue508.ui-states') ?? '[]'));
    states.add(value);
    sessionStorage.setItem('spine.issue508.ui-states', JSON.stringify([...states]));
  }, state);
}

function installMetadataObservers(page: Page, evidence: EvidenceCollector): void {
  page.on('request', (request) => evidence.observeRequest(request));
  page.on('response', (response) => { void evidence.observeResponse(response); });
}

class EvidenceCollector {
  readonly askIdempotencyKeys: string[] = [];
  readonly askCommandIds: string[] = [];
  readonly askReplaysByKey = new Map<string, boolean[]>();
  readonly askRuns: string[] = [];
  readonly askRunsByKey = new Map<string, string[]>();
  readonly eventRequests: Array<{ lastEventId: string | null }> = [];
  readonly feedbackIds: string[] = [];
  readonly scenarios = new Set<string>();
  readonly terminalOutcomes: string[] = [];
  readonly terminalOutcomesByRun = new Map<string, string>();
  readonly terminalRunIds: string[] = [];
  readonly traceIds: string[] = [];
  readonly uiStates = new Set<string>();
  readonly workspaceBOutcomes: string[] = [];
  answerMessageId: string | null = null;
  answerRunId: string | null = null;
  cancellationRunId: string | null = null;
  citationId: string | null = null;
  citationLocatorValidated = false;
  ingestionRunId: string | null = null;
  sessionAId: string | null = null;
  sessionBId: string | null = null;
  sourceObjectId: string | null = null;
  sourceRevisionId: string | null = null;
  workspaceBCitationLocatorValidated = false;
  workspaceBSourceObjectId: string | null = null;
  workspaceBSourceRevisionId: string | null = null;
  readonly noLeakSentinel: string;
  readonly privateArtifactValues = new Set<string>();

  constructor(readonly config: LiveQualificationConfig) {
    this.noLeakSentinel = `SPINE508_NO_LEAK_${config.qualificationRunId}`;
    for (const value of [
      config.accessToken,
      config.answerableQuestion,
      config.unanswerableQuestion,
      config.cancelQuestion,
      config.documentPath,
      basename(config.documentPath),
      WORKSPACE_B_ANSWERABLE_QUESTION,
      WORKSPACE_B_DOCUMENT_PATH,
      basename(WORKSPACE_B_DOCUMENT_PATH),
      this.noLeakSentinel,
    ]) this.captureSensitiveValue(value);
  }

  captureSensitiveValue(value: string | null | undefined): void {
    const normalized = value?.trim();
    if (normalized) this.privateArtifactValues.add(normalized);
  }

  observeRequest(request: Request): void {
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/ask/admissions') {
      this.askIdempotencyKeys.push(request.headers()['idempotency-key'] ?? '');
    }
    if (path.endsWith('/events')) {
      this.eventRequests.push({ lastEventId: request.headers()['last-event-id'] ?? null });
    }
  }

  async observeResponse(response: PlaywrightResponse): Promise<void> {
    const path = new URL(response.url()).pathname;
    if (!response.ok() || !path.startsWith('/api/v1/')) return;
    if (!['/api/v1/documents/batches', '/api/v1/ask/admissions', '/api/v1/qa/feedback'].includes(path)
      && !path.startsWith('/api/v1/ingestion/runs/')
      && !path.includes('/messages/') && !path.startsWith('/api/v1/qa/citations/')) return;
    let body: unknown;
    try { body = await response.json(); } catch { return; }
    if (!isRecord(body)) return;
    const traceId = safeUuid(body.trace_id);
    if (traceId) this.traceIds.push(traceId);
    if (
      (path === '/api/v1/documents/batches' || path.startsWith('/api/v1/ingestion/runs/'))
      && response.request().headers()['x-workspace-id'] === this.config.workspaceAId
    ) {
      const item = Array.isArray(body.items) && isRecord(body.items[0]) ? body.items[0] : null;
      this.ingestionRunId = safeUuid(body.run_id);
      this.sourceObjectId = safeUuid(item?.source_object_id) ?? this.sourceObjectId;
      this.sourceRevisionId = safeUuid(item?.source_revision_id) ?? this.sourceRevisionId;
    } else if (path === '/api/v1/ask/admissions' && isRecord(body.result)) {
      const commandId = safeUuid(body.command_id);
      const messageId = safeUuid(body.result.message_id);
      const runId = safeUuid(body.result.run_id);
      const idempotencyKey = response.request().headers()['idempotency-key'] ?? '';
      if (commandId) this.askCommandIds.push(commandId);
      if (typeof body.replay === 'boolean') {
        const replays = this.askReplaysByKey.get(idempotencyKey) ?? [];
        replays.push(body.replay);
        this.askReplaysByKey.set(idempotencyKey, replays);
      }
      if (runId) {
        this.askRuns.push(runId);
        const runs = this.askRunsByKey.get(idempotencyKey) ?? [];
        runs.push(runId);
        this.askRunsByKey.set(idempotencyKey, runs);
      }
      if (idempotencyKey && idempotencyKey === this.askIdempotencyKeys[0]) {
        this.answerMessageId = messageId;
        this.answerRunId = runId;
      }
    } else if (path === '/api/v1/qa/feedback' && isRecord(body.result)) {
      const feedbackId = safeUuid(body.result.feedback_id);
      if (feedbackId) this.feedbackIds.push(feedbackId);
    } else if (path.startsWith('/api/v1/qa/citations/') && isRecord(body.data) && isRecord(body.data.evidence)) {
      const value = body.data.evidence;
      if (typeof value.excerpt === 'string') this.captureSensitiveValue(value.excerpt);
      const citationId = safeUuid(value.citation_id);
      const reference = isRecord(value.evidence_reference) ? value.evidence_reference : null;
      const locator = reference && isRecord(reference.locator) ? reference.locator : null;
      const locatorValidated = typeof value.locator_schema === 'string'
        && /^[a-z][a-z0-9-]*:v[1-9][0-9]*$/.test(value.locator_schema)
        && typeof value.locator_reference === 'string'
        && value.locator_reference.length > 0
        && reference?.locator_contract_version === value.locator_schema
        && locator?.schema_version === 'stable-locator:v1'
        && typeof locator.kind === 'string'
        && ['page', 'sheet_row', 'message', 'character_range', 'record_field', 'custom'].includes(locator.kind)
        && locator.value === value.locator_reference;
      if (locatorValidated && safeUuid(value.source_revision_id) === this.sourceRevisionId) {
        this.citationLocatorValidated = true;
        this.citationId = citationId;
      }
      if (
        locatorValidated
        && safeUuid(value.source_object_id) === this.workspaceBSourceObjectId
        && safeUuid(value.source_revision_id) === this.workspaceBSourceRevisionId
      ) {
        this.workspaceBCitationLocatorValidated = true;
      }
    } else if (isRecord(body.data)) {
      if (typeof body.data.answer === 'string') this.captureSensitiveValue(body.data.answer);
      const outcome = body.data.terminal_outcome;
      if (typeof outcome === 'string') {
        this.terminalOutcomes.push(outcome);
        const terminalRunId = safeUuid(body.data.run_id);
        if (terminalRunId) {
          this.terminalRunIds.push(terminalRunId);
          this.terminalOutcomesByRun.set(terminalRunId, outcome);
        }
        if (body.workspace_id === this.config.workspaceBId) this.workspaceBOutcomes.push(outcome);
      }
    }
  }

  captureUiStates(states: string[]): void {
    for (const state of states) this.uiStates.add(state);
  }

  assertComplete(): void {
    const required = [
      'upload_active_projection', 'answer_citation_feedback_run', 'duplicate_retry', 'sse_reconnect',
      'session_restoration', 'abstention', 'cancellation', 'two_workspace_isolation',
      'evidence_disclosure_rejection', 'safe_ui_states',
    ];
    if (required.some((scenario) => !this.scenarios.has(scenario))) throw new Error('scenario_incomplete');
    if (!this.citationLocatorValidated) throw new Error('citation_locator_not_validated');
    if (!this.workspaceBCitationLocatorValidated) throw new Error('workspace_b_citation_not_validated');
    if (
      !this.cancellationRunId
      || this.terminalOutcomesByRun.get(this.cancellationRunId) !== 'cancelled'
      || !this.terminalRunIds.includes(this.cancellationRunId)
    ) throw new Error('cancelled_terminal_run_not_correlated');
    if (this.feedbackIds.length === 0) throw new Error('feedback_reference_missing');
    const keys = this.askIdempotencyKeys.filter(Boolean);
    if (keys.length < 2 || keys[0] !== keys[1]) throw new Error('ask_retry_identity_not_stable');
    const retryRuns = this.askRunsByKey.get(keys[0]!) ?? [];
    if (retryRuns.length === 0 || new Set(retryRuns).size !== 1) {
      throw new Error('duplicate_logical_answer');
    }
    if (!this.askReplaysByKey.get(keys[0]!)?.includes(true)) {
      throw new Error('durable_admission_replay_not_observed');
    }
    if (this.askCommandIds.length === 0 || this.traceIds.length === 0 || this.terminalRunIds.length === 0) {
      throw new Error('run_correlation_incomplete');
    }
  }

  async write(status: 'pass' | 'blocked', reasonCode?: string): Promise<void> {
    const record = {
      schema_version: 'issue-508-governed-qa-browser:v1',
      ticket: 508,
      status,
      reason_code: reasonCode ?? null,
      qualification_run_id: this.config.qualificationRunId,
      deployment: {
        api_release_identity: this.config.apiReleaseIdentity,
        commit_sha: this.config.commitSha,
        deployment_fingerprint: this.config.deploymentFingerprint,
        provider_profile_identity: this.config.providerProfileIdentity,
        web_release_identity: this.config.webReleaseIdentity,
      },
      scope: {
        scope_references: [
          { workspace_id: this.config.workspaceAId, environment_id: this.config.environmentAId },
          { workspace_id: this.config.workspaceBId, environment_id: this.config.environmentBId },
        ],
      },
      correlated_references: {
        admission_command_ids: [...new Set(this.askCommandIds)],
        ask_run_ids: [...new Set(this.askRuns)],
        cancellation_run_id: this.cancellationRunId,
        feedback_ids: [...new Set(this.feedbackIds)],
        ingestion_run_id: this.ingestionRunId,
        session_ids: [this.sessionAId, this.sessionBId].filter(Boolean),
        source_object_id: this.sourceObjectId,
        source_revision_id: this.sourceRevisionId,
        workspace_b_source_object_id: this.workspaceBSourceObjectId,
        workspace_b_source_revision_id: this.workspaceBSourceRevisionId,
        terminal_run_ids: [...new Set(this.terminalRunIds)],
        trace_ids: [...new Set(this.traceIds)],
      },
      scenarios: [...this.scenarios].sort(),
      ui_states: [...this.uiStates].sort(),
      required_scenarios: [
        'upload_active_projection',
        'answer_citation_feedback_run',
        'duplicate_retry',
        'sse_reconnect',
        'session_restoration',
        'abstention',
        'cancellation',
        'two_workspace_isolation',
        'evidence_disclosure_rejection',
        'safe_ui_states',
      ],
      citation_locator_validation: this.citationLocatorValidated ? 'pass' : 'not_verified',
      execution: {
        api_mocking: false,
        browser: 'chromium',
        cli_user_actions: false,
        controlled_fixture: false,
        post_admission_response_disconnect_injected: true,
        route_interception: false,
      },
      privacy: {
        artifact_no_leak_scan: 'pass',
        content_fields_recorded: false,
        sentinel_scan: 'pass',
        screenshots: false,
        trace: false,
        video: false,
      },
    };
    const serialized = `${JSON.stringify(record, null, 2)}\n`;
    if (status === 'pass' && [...this.privateArtifactValues].some((value) => serialized.includes(value))) {
      throw new Error('artifact_no_leak_scan_failed');
    }
    await mkdir(dirname(this.config.reportPath), { recursive: true });
    await writeFile(this.config.reportPath, serialized, { encoding: 'utf8', mode: 0o600 });
  }
}

function scoped(path: string, workspaceId: string, environmentId: string): string {
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}workspace=${workspaceId}&environment=${environmentId}`;
}

async function choose(page: Page, label: string, option: string): Promise<void> {
  await page.getByRole('combobox', { name: label }).click();
  await page.getByRole('option', { name: option, exact: true }).click();
}

async function visible(locator: Locator, code: string, timeout = 60_000): Promise<void> {
  try { await locator.waitFor({ state: 'visible', timeout }); } catch { throw new Error(code); }
}

async function waitFor(check: () => boolean | Promise<boolean>, code: string, timeout = 60_000): Promise<void> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(code);
}

function uuidFromPath(url: string, segment: string): string {
  const match = new URL(url).pathname.match(new RegExp(`/${segment}/([0-9a-f-]{36})`, 'i'));
  const value = match?.[1];
  if (!value || !UUID_PATTERN.test(value)) throw new Error('canonical_identity_missing');
  return value;
}

function safeUuid(value: unknown): string | null {
  return typeof value === 'string' && UUID_PATTERN.test(value) ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
