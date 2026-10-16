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
  await expect(page.getByRole('heading', { name: 'Документ' })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Запуск обработки' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Запуск обработки' })).toBeVisible();

  expect(uploadRequests).toHaveLength(1);
  expect(uploadRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(uploadRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(uploadRequests[0]?.headers['idempotency-key']).toMatch(/^batch-upload-/);
  expect(uploadRequests[0]?.body).toContain('proposal.txt');
  expect(uploadRequests[0]?.body).toContain('context.md');
});

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
      identity: {
        identity_mode: 'upload',
        upload_identity: 'secret.pdf',
      },
      operational_metadata: {
        lifecycle: 'current',
        processing_state: 'processed',
        publication_status: 'published',
        schema_version: 1 as const,
        source_object_id: sourceObjectId,
        status: 'available',
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
