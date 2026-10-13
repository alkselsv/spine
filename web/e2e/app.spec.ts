import { expect, test, type Page } from '@playwright/test';

const primaryWorkspace = '10000000-0000-4000-8000-000000000001';
const salesWorkspace = '10000000-0000-4000-8000-000000000002';
const localEnvironment = '20000000-0000-4000-8000-000000000001';
const stagingEnvironment = '20000000-0000-4000-8000-000000000002';
const traceId = '50000000-0000-4000-8000-000000000001';

test('test_control_plane_navigation_preserves_scope_and_generated_client_context', async ({ page }) => {
  const healthRequests = await installControlledHealth(page);

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
  expect(healthRequests.every(({ url }) => !url.includes('controlled-browser-token'))).toBe(true);
  await expect(page.getByRole('alert')).not.toContainText(/customer|protected|resource_exists/i);
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
