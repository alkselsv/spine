import { expect, test, type Page } from '@playwright/test';
import type { components } from '../api/generated/schema';

const primaryWorkspace = '10000000-0000-4000-8000-000000000001';
const salesWorkspace = '10000000-0000-4000-8000-000000000002';
const localEnvironment = '20000000-0000-4000-8000-000000000001';
const stagingEnvironment = '20000000-0000-4000-8000-000000000002';
const traceId = '50000000-0000-4000-8000-000000000001';
const firstDocument = '30000000-0000-4000-8000-000000000001';
const secondDocument = '30000000-0000-4000-8000-000000000002';

test('test_control_plane_navigation_preserves_scope_and_generated_client_context', async ({ page }) => {
  const healthRequests = await installControlledHealth(page);
  const documentRequests = await installControlledDocuments(page);

  await page.goto('/?fixture=controlled');
  await expect(page).toHaveTitle('Spine Control Plane');
  await expect(page.getByRole('heading', { name: 'Control Plane Spine' })).toBeVisible();
  await page.getByRole('button', { name: 'Проверить соединение' }).click();
  await expect(page.getByRole('status')).toContainText('Соединение проверено');

  await choose(page, 'Рабочая область', 'Продажи');
  await choose(page, 'Среда', 'Staging');
  await expect(page).toHaveURL(
    new RegExp(`workspace=${salesWorkspace}&environment=${stagingEnvironment}`),
  );

  await page.getByRole('link', { name: 'Знания', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/knowledge/documents\\?`));
  await expect(page.getByRole('heading', { name: 'Документы', exact: true })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Рабочая область' })).toContainText('Продажи');
  await expect(page.getByRole('combobox', { name: 'Среда' })).toContainText('Staging');

  await page.reload();
  await expect(page.getByRole('combobox', { name: 'Рабочая область' })).toContainText('Продажи');
  await expect(page.getByRole('combobox', { name: 'Среда' })).toContainText('Staging');

  await page.getByRole('button', { name: 'Проверить соединение' }).click();
  await expect(page.getByRole('alert')).toHaveText(/Доступ запрещён/);
  expect(healthRequests).toHaveLength(2);
  expect(healthRequests[0]?.headers.authorization).toBe('Bearer controlled-browser-token');
  expect(healthRequests[0]?.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(healthRequests[0]?.headers['x-environment-id']).toBe(localEnvironment);
  expect(healthRequests[1]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(healthRequests[1]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(documentRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(documentRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(healthRequests.every(({ url }) => !url.includes('controlled-browser-token'))).toBe(true);
  await expect(page.getByRole('alert')).not.toContainText(/customer|protected|resource_exists/i);
});

test('test_documents_discovery_filters_paginates_and_navigates_without_content_identity', async ({ page }) => {
  const documentRequests = await installControlledDocuments(page);
  await page.goto(
    `/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );

  await expect(page.getByRole('heading', { name: 'Документы', exact: true })).toBeVisible();
  await expect(page.getByRole('table')).toBeVisible();
  await expect(page.getByText(`ID: ${firstDocument}`)).toBeVisible();
  await expect(page.getByRole('group', { name: 'Доступ и безопасные метаданные' })).toBeVisible();
  await expect(page.getByText('security-domain:controlled')).toBeVisible();
  await expect(page.getByText('r1')).toBeVisible();
  await expect(page.getByRole('group', { name: 'Доступ и безопасные метаданные' })).toContainText(
    'Поколение AccessPolicy: 3',
  );
  await expect(page.getByText(traceId)).toBeVisible();
  await expect(page.getByText(salesWorkspace)).toBeVisible();
  await expect(page.locator('body')).not.toContainText(/secret\.pdf|protected title|checksum/i);

  await page.getByRole('combobox', { name: 'Публикация' }).click();
  await page.getByRole('option', { name: 'Опубликован', exact: true }).click();
  await expect(page).toHaveURL(/publication_status=published/);
  await page.getByRole('button', { name: 'Следующая страница' }).click();
  await expect(page).toHaveURL(/cursor=documents-next/);
  await expect(page.getByText(`ID: ${secondDocument}`)).toBeVisible();

  await page.getByRole('link', { name: 'Открыть' }).click();
  await expect(page).toHaveURL(
    new RegExp(`/knowledge/documents/${secondDocument}\\?.*workspace=${salesWorkspace}.*publication_status=published.*cursor=documents-next`),
  );
  await expect(page.getByRole('heading', { name: 'Операционный обзор документа', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Вернуться к документам' }).click();
  await expect(page.getByText(`ID: ${secondDocument}`)).toBeVisible();
  await page.reload();
  await expect(page.getByText(`ID: ${secondDocument}`)).toBeVisible();

  await choose(page, 'Рабочая область', 'Основная');
  await choose(page, 'Среда', 'Local');
  await expect(page).toHaveURL(new RegExp(`workspace=${primaryWorkspace}&environment=${localEnvironment}`));
  await expect(page.getByText(`ID: ${firstDocument}`)).toBeVisible();

  expect(documentRequests.length).toBeGreaterThanOrEqual(2);
  expect(documentRequests.every(({ headers }) => headers.authorization === 'Bearer controlled-browser-token')).toBe(true);
  expect(documentRequests.some(({ headers }) => headers['x-workspace-id'] === salesWorkspace && headers['x-environment-id'] === stagingEnvironment)).toBe(true);
  expect(documentRequests.some(({ headers }) => headers['x-workspace-id'] === primaryWorkspace && headers['x-environment-id'] === localEnvironment)).toBe(true);
  expect(documentRequests.every(({ url }) => !url.includes('unsupported'))).toBe(true);
});

test('test_documents_upload_reviews_batch_polls_processing_and_links_run', async ({ page }) => {
  await installControlledDocuments(page);
  const uploadRequests = await installControlledBatchUpload(page);
  await installControlledRunReadModels(page);

  await page.goto(
    `/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );
  await page.locator('[aria-label="Перетащите файлы сюда"]').evaluate((element) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File(['synthetic upload fixture'], 'proposal.txt', { type: 'text/plain' }));
    dataTransfer.items.add(new File(['synthetic markdown fixture'], 'context.md', { type: 'text/markdown' }));
    element.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer }));
  });

  await expect(page.getByRole('region', { name: 'Проверка перед отправкой' })).toBeVisible();
  await expect(page.getByText('Файлов в очереди: 2')).toBeVisible();
  await page.getByRole('button', { name: 'Загрузить документы' }).click();
  await expect(page.getByRole('listitem').filter({ hasText: 'Готов' })).toHaveCount(2);
  await expect(page.getByRole('link', { name: 'Открыть запуск обработки' })).toHaveAttribute(
    'href',
    new RegExp(`/runs/${firstDocument}.*workspace=${salesWorkspace}.*environment=${stagingEnvironment}`),
  );
  await page.getByRole('link', { name: 'Открыть запуск обработки' }).click();
  await expect(page.getByRole('heading', { name: 'Запуск', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Запуски обработки' })).toBeVisible();
  await expect(page.getByRole('list', { name: 'Результаты файлов' }).getByText('Готов')).toHaveCount(2);
  await expect(page.getByRole('heading', { name: 'История запусков' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'История обработки файлов' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Запуск', exact: true })).toBeVisible();
  await expect(page.getByRole('list', { name: 'Результаты файлов' }).getByText('Готов')).toHaveCount(2);

  expect(uploadRequests).toHaveLength(1);
  expect(uploadRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(uploadRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(uploadRequests[0]?.headers['idempotency-key']).toMatch(/^batch-upload-/);
  expect(uploadRequests[0]?.body).toContain('proposal.txt');
  expect(uploadRequests[0]?.body).toContain('context.md');
});

test('test_ingestion_run_displays_partial_outcomes_retryability_and_safe_diagnostics', async ({ page }) => {
  await installControlledRunReadModels(page);
  await page.route(`**/api/v1/ingestion/runs/${firstDocument}`, async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(controlledPartialRunStatus()),
      status: 200,
    });
  });

  await page.goto(
    `/runs/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );

  await expect(page.getByRole('heading', { name: 'Запуск', exact: true })).toBeVisible();
  await expect(page.getByText('Готов')).toBeVisible();
  await expect(page.getByRole('listitem').filter({ hasText: 'failed.txt' })).toContainText('Ошибка');
  await expect(page.getByText('Ошибка: parser.invalid (parse)')).toBeVisible();
  await expect(page.getByText('Повтор операции разрешён', { exact: true })).toBeVisible();
  await expect(page.getByText('Следующее действие: Повтор операции разрешён.')).toBeVisible();
  await expect(page.locator('body')).not.toContainText(/provider|secret|stack|customer/i);
});

async function installControlledRunReadModels(page: Page) {
  await page.route('**/api/v1/ingestion/runs', async (route) => {
    const request = route.request();
    const headers = request.headers();
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        environment_id: headers['x-environment-id'],
        items: [
          {
            acceptance: 'accepted',
            items: [controlledHistoryItem()],
            processing: 'succeeded',
            requested_at: '2026-01-02T03:04:05Z',
            run_id: firstDocument,
            run_state: 'completed',
            status: 'succeeded',
            status_url: `/api/v1/ingestion/runs/${firstDocument}`,
            terminal: true,
          },
        ],
        limit: 25,
        next_cursor: null,
        schema_version: 1,
        trace_id: traceId,
        workspace_id: headers['x-workspace-id'],
      }),
      status: 200,
    });
  });
  await page.route('**/api/v1/documents/processing-history', async (route) => {
    const request = route.request();
    const headers = request.headers();
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        environment_id: headers['x-environment-id'],
        items: [controlledHistoryItem()],
        limit: 25,
        next_cursor: null,
        schema_version: 1,
        trace_id: traceId,
        workspace_id: headers['x-workspace-id'],
      }),
      status: 200,
    });
  });
}

test('test_documents_upload_rejects_invalid_file_before_network_submission', async ({ page }) => {
  await installControlledDocuments(page);
  const uploadRequests = await installControlledBatchUpload(page);

  await page.goto(
    `/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );
  await page.locator('input[type="file"]').setInputFiles({
    name: 'protected.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('synthetic invalid fixture'),
  });

  await expect(page.getByRole('alert')).toContainText('неподдерживаемый тип');
  expect(uploadRequests).toHaveLength(0);
});

test('test_document_detail_deep_link_reloads_and_preserves_scope_and_subview_navigation', async ({ page }) => {
  const documentRequests = await installControlledDocumentDetail(page);
  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );

  await expect(page.getByRole('heading', { name: 'Операционный обзор документа' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Доступ и безопасные метаданные' })).toBeVisible();
  await expect(page.getByText('security-domain:controlled')).toBeVisible();
  await expect(page.getByText(traceId)).toBeVisible();
  await expect(page.locator('header').getByText('Идентификатор источника')).toBeVisible();
  await expect(page.locator('header code')).toHaveText(firstDocument);
  await expect(page.locator('body')).not.toContainText(/secret\.pdf|protected title|checksum/i);
  await expect(page.getByRole('link', { name: 'История ревизий' })).toHaveAttribute(
    'href',
    new RegExp(`/knowledge/documents/${firstDocument}.*workspace=${salesWorkspace}.*environment=${stagingEnvironment}.*tab=revisions`),
  );

  await page.getByRole('link', { name: 'История ревизий' }).click();
  await expect(page).toHaveURL(/tab=revisions/);
  await expect(page.getByRole('heading', { name: 'История ревизий' })).toBeVisible();
  await expect(page.getByRole('link', { name: secondDocument })).toHaveAttribute('aria-current', 'true');
  await page.reload();
  await expect(page).toHaveURL(/tab=revisions/);
  await expect(page.getByRole('link', { name: 'История ревизий' })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('heading', { name: 'История ревизий' })).toBeVisible();
  await expect(page.getByRole('link', { name: secondDocument })).toHaveAttribute('aria-current', 'true');

  await expect(page.getByRole('link', { name: 'Открыть запуск обработки' })).toHaveAttribute(
    'href',
    new RegExp(`/runs/${firstDocument}.*workspace=${salesWorkspace}.*environment=${stagingEnvironment}.*document=${firstDocument}`),
  );
  await expect(page.getByRole('link', { name: 'Панель проекции' })).toHaveAttribute(
    'href',
    new RegExp(`/knowledge/documents/${firstDocument}.*tab=projection`),
  );

  expect(documentRequests).toHaveLength(4);
  expect(documentRequests.every(({ headers }) => headers.authorization === 'Bearer controlled-browser-token')).toBe(true);
  expect(documentRequests.every(({ headers }) => headers['x-workspace-id'] === salesWorkspace)).toBe(true);
  expect(documentRequests.every(({ headers }) => headers['x-environment-id'] === stagingEnvironment)).toBe(true);
});

test('test_projection_status_scope_switch_reload_and_safe_degradation', async ({ page }) => {
  await installControlledDocumentDetail(page);
  const projectionRequests: Array<{ headers: Record<string, string>; url: string }> = [];
  await page.route('**/api/v1/projections/*/status', async (route) => {
    const request = route.request();
    const headers = request.headers();
    projectionRequests.push({ headers, url: request.url() });
    const unavailable = headers['x-workspace-id'] === primaryWorkspace;
    const status: components['schemas']['ProjectionStatusResponse'] = unavailable
      ? {
          projection_kind: 'qa',
          publication_status: 'unavailable',
          reason_code: 'projection.publication_unavailable',
          recovery_state: 'unavailable',
          retryable: false,
          schema_version: 1,
        }
      : {
          activation_generation: 12,
          as_of: {
            accepted_through_generation: 81,
            boundary_time: '2026-01-02T03:04:05Z',
            canonical_boundary_id: secondDocument,
          },
          canonical_boundary_id: secondDocument,
          drift_count: 2,
          freshness: 'stale',
          health: 'degraded',
          last_operation: {
            operation: 'reconciliation',
            retryable: false,
            status: 'failed',
          },
          projection_config_version: 4,
          projection_config_version_id: secondDocument,
          projection_kind: 'qa',
          publication_status: 'published',
          reason_code: 'projection.reconciliation_failed',
          reconciliation: {
            checked_at: '2026-01-02T03:04:05Z',
            outcome: 'failed',
            result_id: firstDocument,
          },
          recovery_state: 'failed',
          retryable: false,
          schema_version: 1,
          snapshot_id: firstDocument,
        };
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        data: status,
        environment_id: headers['x-environment-id'],
        schema_version: 1,
        trace_id: traceId,
        workspace_id: headers['x-workspace-id'],
      }),
      status: 200,
    });
  });
  await page.route('**/api/v1/health/summary', async (route) => {
    const headers = route.request().headers();
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        data: {
          authorization: { code: 'authorization.ready', status: 'ready' },
          build_version: 'controlled-build',
          configuration_version: 'controlled-config',
          ingestion: { code: 'ingestion.ready', status: 'ready' },
          providers: { database: 'must not be rendered' },
          publication: { code: 'publication.degraded', status: 'degraded' },
          status: 'degraded',
        },
        environment_id: headers['x-environment-id'],
        schema_version: 1,
        trace_id: traceId,
        workspace_id: headers['x-workspace-id'],
      }),
      status: 200,
    });
  });

  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}&tab=projection&projection_kind=qa`,
  );
  const panel = page.getByRole('region', { name: 'Состояние публикации проекции' });
  await expect(panel.getByText('Устарела')).toBeVisible();
  await expect(panel.getByText('Ограничен')).toHaveCount(3);
  await expect(panel.getByRole('button', { name: 'Запросить пересборку проекции' })).toBeVisible();
  await expect(page.getByText(/не подтверждает доступ к доказательствам/i)).toBeVisible();
  await expect(page.locator('body')).not.toContainText('must not be rendered');
  await expect(page).toHaveURL(/tab=projection.*projection_kind=qa/);

  await choose(page, 'Рабочая область', 'Основная');
  await expect(page).toHaveURL(new RegExp(`workspace=${primaryWorkspace}.*tab=projection.*projection_kind=qa`));
  await expect(panel.getByText('Недоступен')).toHaveCount(2);
  await page.reload();
  await expect(panel.getByText('Недоступен')).toHaveCount(2);
  expect(projectionRequests.length).toBeGreaterThanOrEqual(3);
  expect(projectionRequests.every(({ url }) => url.includes('/projections/qa/status'))).toBe(true);
  expect(projectionRequests.some(({ headers }) => headers['x-workspace-id'] === salesWorkspace)).toBe(true);
  expect(projectionRequests.some(({ headers }) => headers['x-workspace-id'] === primaryWorkspace)).toBe(true);
  expect(projectionRequests.every(({ headers }) => headers['x-environment-id'] === stagingEnvironment)).toBe(true);
});

test('test_document_revision_history_direct_url_reloads_exact_selected_revision', async ({ page }) => {
  const requests = await installControlledDocumentDetail(page);
  const historyResponse = page.waitForResponse((response) => response.url().includes('/revisions'));
  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}&tab=revisions&revision=${secondDocument}`,
  );

  await expect(page.getByRole('heading', { name: 'История ревизий' })).toBeVisible();
  expect((await historyResponse).status()).toBe(200);
  await expect(page.getByRole('link', { name: secondDocument })).toHaveAttribute('aria-current', 'true');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'История ревизий' })).toBeVisible();
  await expect(page.getByRole('link', { name: secondDocument })).toHaveAttribute('aria-current', 'true');
  await expect(page).toHaveURL(new RegExp(`tab=revisions.*revision=${secondDocument}`));

  expect(requests).toHaveLength(4);
  expect(requests.every(({ headers }) => headers['x-workspace-id'] === salesWorkspace)).toBe(true);
  expect(requests.every(({ headers }) => headers['x-environment-id'] === stagingEnvironment)).toBe(true);
});

test('test_authentication_denial_hides_administrator_shell_without_api_request', async ({ page }) => {
  let requestCount = 0;
  await page.route('**/api/v1/**', async (route) => {
    requestCount += 1;
    await route.continue();
  });

  await page.goto('/?fixture=auth-denied');

  await expect(page.getByRole('heading', { name: 'Доступ запрещён' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Control Plane Spine' })).not.toBeVisible();
  expect(requestCount).toBe(0);
  await expect(page.locator('body')).not.toContainText(/provider|secret|stack|customer/i);
});

test('test_chat_fixture_reconnects_and_rehydrates_canonical_message', async ({ page }) => {
  await page.goto(
    `/chat/demo-session?fixture=controlled&workspace=${primaryWorkspace}&environment=${localEnvironment}`,
  );

  await expect(page.getByRole('heading', { name: 'Диагностический чат' })).toBeVisible();
  await expect(page.getByText('Канонический ответ из controlled fixture.')).toBeVisible();

  await page.getByLabel('Вопрос').fill('Проверить controlled SSE');
  await page.getByRole('button', { name: 'Отправить' }).click();

  await expect(page.getByText(/SSE переподключение \(1\)/)).toBeVisible();
  await expect(page.getByText('Ответ восстановлен')).toBeVisible();
  await expect(page.getByText('Канонический ответ из controlled fixture.')).toHaveCount(1);
  await expect(page.getByLabel('События SSE')).toContainText('completed');
});

test('test_chat_fixture_cancels_and_retries_same_command_without_duplicate_message', async ({ page }) => {
  await page.goto(
    `/chat/demo-session?fixture=controlled&workspace=${primaryWorkspace}&environment=${localEnvironment}`,
  );

  await page.getByLabel('Вопрос').fill('Отменить controlled run');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await page.getByRole('button', { name: 'Отменить' }).click();

  await expect(page.getByText('Операция отменена')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Повторить' })).toBeVisible();
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('Операция отменена')).toBeVisible();
  await expect(page.getByLabel('Канонические сообщения').getByRole('article')).toHaveCount(1);
});

async function choose(page: Page, label: string, option: string) {
  await page.getByRole('combobox', { name: label }).click();
  await page.getByRole('option', { name: option, exact: true }).click();
}

async function installControlledHealth(page: Page) {
  const requests: Array<{ url: string; headers: Record<string, string> }> = [];
  await page.route('**/api/v1/health/ready', async (route) => {
    const request = route.request();
    const headers = request.headers();
    requests.push({ url: request.url(), headers });
    const allowed =
      headers['x-workspace-id'] === primaryWorkspace &&
      headers['x-environment-id'] === localEnvironment;
    if (allowed) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ready' }),
        status: 200,
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'spine.authorization.denied',
        safe_message: 'Authorization is denied.',
        retryability: 'never',
        schema_version: 1,
        trace_id: traceId,
        protected_text: 'customer secret must not leak',
        resource_exists: true,
      }),
      status: 403,
    });
  });
  return requests;
}

async function installControlledDocuments(page: Page) {
  const requests: Array<{ url: string; headers: Record<string, string> }> = [];
  await page.route('**/api/v1/documents*', async (route) => {
    const request = route.request();
    const headers = request.headers();
    requests.push({ url: request.url(), headers });
    const workspaceHeader = headers['x-workspace-id'];
    const environmentHeader = headers['x-environment-id'];
    if (!workspaceHeader || !environmentHeader) throw new Error('Controlled scope headers are missing.');
    const url = new URL(request.url());
    const isSecondPage = url.searchParams.get('cursor') === 'documents-next';
    const sourceObjectId = isSecondPage ? secondDocument : firstDocument;
    const item = {
      created_at: '2026-01-02T03:04:05Z',
      operational_metadata: {
        access_policy: {
          generation: 3,
          policy_version_id: '51000000-0000-0000-0000-000000000025',
          schema_version: 1 as const,
          version: 2,
        },
        lifecycle: 'current',
        processing_state: 'processed',
        publication_status: 'published',
        schema_version: 1 as const,
        source_object_id: sourceObjectId,
        status: 'available',
        security_domain: {
          binding_version: 1,
          reference: 'security-domain:controlled',
          schema_version: 1 as const,
          version: 'r1',
        },
      },
      publication: {
        freshness: 'current',
        health: 'healthy',
        projection_kind: 'qa',
        status: 'published',
      },
      schema_version: 1 as const,
      source_kind: 'document',
      source_object_id: sourceObjectId,
    } satisfies components['schemas']['DocumentSnapshot'];
    const body = {
      environment_id: environmentHeader,
      items: [item],
      limit: 25,
      next_cursor: isSecondPage ? null : 'documents-next',
      schema_version: 1 as const,
      trace_id: traceId,
      workspace_id: workspaceHeader,
    } satisfies components['schemas']['ApiCollectionEnvelope_DocumentSnapshot_'];
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(body),
      status: 200,
    });
  });
  return requests;
}

async function installControlledDocumentDetail(page: Page) {
  const requests: Array<{ url: string; headers: Record<string, string> }> = [];
  await page.route(`**/api/v1/documents/${firstDocument}`, async (route) => {
    const request = route.request();
    requests.push({ url: request.url(), headers: request.headers() });
    const headers = request.headers();
    const snapshot = {
      created_at: '2026-01-02T03:04:05Z',
      current_revision: {
        accepted_at: '2026-01-02T04:00:00Z',
        accepted_generation: 2,
        admission_status: 'admitted' as const,
        kind: 'content' as const,
        lifecycle: 'current' as const,
        observed_at: '2026-01-02T03:00:00Z',
        processing_references: [
          {
            coverage: 'complete' as const,
            ingestion_item_id: secondDocument,
            ingestion_run_id: firstDocument,
            processing_receipt_id: null,
            state: 'processed' as const,
          },
        ],
        projection_references: [],
        schema_version: 1 as const,
        source_revision_id: secondDocument,
        predecessor_revision_id: null,
        superseded_by_revision_id: null,
      },
      operational_metadata: {
        access_policy: {
          generation: 3,
          policy_version_id: '51000000-0000-0000-0000-000000000025',
          schema_version: 1 as const,
          version: 2,
        },
        freshness: 'current' as const,
        health: 'healthy' as const,
        lifecycle: 'current' as const,
        processing_state: 'processed' as const,
        publication_status: 'published' as const,
        schema_version: 1 as const,
        source_object_id: firstDocument,
        source_revision_id: secondDocument,
        status: 'available' as const,
        security_domain: {
          binding_version: 1,
          reference: 'security-domain:controlled',
          schema_version: 1 as const,
          version: 'r1',
        },
      },
      publication: {
        freshness: 'current' as const,
        health: 'healthy' as const,
        projection_kind: 'qa',
        status: 'published' as const,
      },
      schema_version: 1 as const,
      source_kind: 'document',
      source_object_id: firstDocument,
    } satisfies components['schemas']['DocumentSnapshot'];
    const response = {
      data: snapshot,
      environment_id: headers['x-environment-id'] ?? '',
      schema_version: 1 as const,
      trace_id: traceId,
      workspace_id: headers['x-workspace-id'] ?? '',
    } satisfies components['schemas']['ApiResponseEnvelope_DocumentSnapshot_'];
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(response), status: 200 });
  });
  await page.route(`**/api/v1/documents/${firstDocument}/revisions*`, async (route) => {
    const request = route.request();
    requests.push({ url: request.url(), headers: request.headers() });
    const headers = request.headers();
    const revision = {
      accepted_at: '2026-01-02T04:00:00Z',
      accepted_generation: 2,
      admission_status: 'admitted' as const,
      content: null,
      deletion_reason: null,
      kind: 'content' as const,
      lifecycle: 'current' as const,
      observed_at: '2026-01-02T03:00:00Z',
      predecessor_revision_id: null,
      processing_references: [],
      projection_references: [],
      schema_version: 1 as const,
      source_revision_id: secondDocument,
      superseded_by_revision_id: null,
    } satisfies components['schemas']['RevisionSnapshot'];
    const response = {
      environment_id: headers['x-environment-id'] ?? '',
      items: [revision],
      limit: 25,
      next_cursor: null,
      schema_version: 1 as const,
      trace_id: traceId,
      workspace_id: headers['x-workspace-id'] ?? '',
    } satisfies components['schemas']['ApiCollectionEnvelope_RevisionSnapshot_'];
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(response), status: 200 });
  });
  return requests;
}

async function installControlledBatchUpload(page: Page) {
  const requests: Array<{
    body: string;
    headers: Record<string, string>;
    url: string;
  }> = [];
  let statusRequests = 0;
  await page.route('**/api/v1/documents/batches', async (route) => {
    const request = route.request();
    requests.push({
      body: request.postData() ?? '',
      headers: request.headers(),
      url: request.url(),
    });
    const response = {
      items: [
        controlledUploadItem({ input_identity: 'file-1' }),
        controlledUploadItem({ input_identity: 'file-2', input_ordinal: 2, item_id: secondDocument }),
      ],
      replay: false,
      run_id: firstDocument,
      run_state: 'running',
      schema_version: 1 as const,
      status: 'processing' as const,
      status_url: `/api/v1/ingestion/runs/${firstDocument}`,
      terminal: false,
    } satisfies components['schemas']['BatchIngestionUploadResponse'];
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(response),
      status: 202,
    });
  });
  await page.route(`**/api/v1/ingestion/runs/${firstDocument}`, async (route) => {
    statusRequests += 1;
    const processing = statusRequests === 1;
    const response = {
      items: [
        controlledUploadItem({
          input_identity: 'file-1',
          item_state: processing ? 'processing' : 'processed',
          processing: processing ? 'processing' : 'succeeded',
          status: processing ? 'non_terminal' : 'succeeded',
          terminal: !processing,
        }),
        controlledUploadItem({
          input_identity: 'file-2',
          input_ordinal: 2,
          item_id: secondDocument,
          item_state: processing ? 'processing' : 'processed',
          processing: processing ? 'processing' : 'succeeded',
          status: processing ? 'non_terminal' : 'succeeded',
          terminal: !processing,
        }),
      ],
      run_id: firstDocument,
      run_state: processing ? 'running' : 'completed',
      schema_version: 1 as const,
      status: processing ? 'processing' : 'succeeded',
      terminal: !processing,
    } satisfies components['schemas']['BatchIngestionStatusResponse'];
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(response),
      status: 200,
    });
  });
  return requests;
}

function controlledUploadItem(
  overrides: Partial<components['schemas']['BatchIngestionItemResponse']> = {},
): components['schemas']['BatchIngestionItemResponse'] {
  return {
    acceptance: 'accepted',
    attempt: 1,
    coverage: null,
    failure_ids: [],
    failures: [],
    input_identity: 'file-1',
    input_ordinal: 1,
    item_id: firstDocument,
    item_state: 'accepted',
    processing: 'not_started',
    processing_receipt: null,
    retryable: false,
    source_object_id: firstDocument,
    source_revision_id: null,
    status: 'non_terminal',
    terminal: false,
    warning_ids: [],
    warnings: [],
    ...overrides,
  };
}

function controlledHistoryItem(
  overrides: Partial<components['schemas']['DocumentProcessingHistoryResponse']> = {},
): components['schemas']['DocumentProcessingHistoryResponse'] {
  return {
    ...controlledUploadItem({
      input_identity: 'file-1',
      input_ordinal: 1,
      item_state: 'processed',
      processing: 'succeeded',
      status: 'succeeded',
      terminal: true,
    }),
    run_id: firstDocument,
    run_state: 'completed',
    status_url: `/api/v1/ingestion/runs/${firstDocument}`,
    ...overrides,
  };
}

function controlledPartialRunStatus(): components['schemas']['BatchIngestionStatusResponse'] {
  return {
    items: [
      controlledUploadItem({
        coverage: 'complete',
        input_identity: 'ready.txt',
        item_state: 'processed',
        processing: 'succeeded',
        status: 'succeeded',
        terminal: true,
      }),
      controlledUploadItem({
        acceptance: 'accepted',
        attempt: 2,
        coverage: 'failed',
        failure_ids: ['60000000-0000-0000-0000-000000000002'],
        failures: [
          {
            attempt: 2,
            code: 'parser.invalid',
            failure_id: '60000000-0000-0000-0000-000000000002',
            retryable: true,
            stage: 'parse',
          },
        ],
        input_identity: 'failed.txt',
        input_ordinal: 2,
        item_id: secondDocument,
        item_state: 'processing_failed',
        processing: 'failed',
        processing_receipt: {
          artifact_digest: null,
          artifact_id: null,
          coverage: 'failed',
          parser_name: 'markdown-v2',
          receipt_id: '60000000-0000-4000-8000-000000000003',
        },
        retryable: true,
        status: 'failed',
        terminal: true,
        warning_ids: ['60000000-0000-0000-0000-000000000004'],
        warnings: [
          {
            attempt: 2,
            code: 'parser.warning',
            stage: 'parse',
            warning_id: '60000000-0000-0000-0000-000000000004',
          },
        ],
      }),
    ],
    run_id: firstDocument,
    run_state: 'completed',
    schema_version: 1,
    status: 'partial',
    terminal: true,
  };
}
