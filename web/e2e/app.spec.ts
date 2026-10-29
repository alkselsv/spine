import { expect, test, type Page, type Route } from '@playwright/test';
import { createHash } from 'node:crypto';
import type { components } from '../api/generated/schema';

const primaryWorkspace = '10000000-0000-4000-8000-000000000001';
const salesWorkspace = '10000000-0000-4000-8000-000000000002';
const localEnvironment = '20000000-0000-4000-8000-000000000001';
const stagingEnvironment = '20000000-0000-4000-8000-000000000002';
const traceId = '50000000-0000-4000-8000-000000000001';
const firstDocument = '30000000-0000-4000-8000-000000000001';
const secondDocument = '30000000-0000-4000-8000-000000000002';
const primaryDocument = '30000000-0000-4000-8000-000000000003';
const primaryRevision = '30000000-0000-4000-8000-000000000004';
const chatSession = '90000000-0000-4000-8000-000000000001';
const chatMessage = '90000000-0000-4000-8000-000000000002';
const chatRun = '90000000-0000-4000-8000-000000000003';
const projectionRun = 'a0000000-0000-4000-8000-000000000001';
const chatCommand = '90000000-0000-4000-8000-000000000004';
const chatCitation = '90000000-0000-4000-8000-000000000005';
const chatClaim = '90000000-0000-4000-8000-000000000006';
const chatRelationship = '90000000-0000-4000-8000-000000000007';
const chatAnswer = '90000000-0000-4000-8000-000000000008';
const chatAttempt = '90000000-0000-4000-8000-000000000009';
const workflowRun = 'b0000000-0000-4000-8000-000000000001';
const requirementsArtifact = 'b0000000-0000-4000-8000-000000000002';

async function installContainerRuntime(page: Page): Promise<void> {
  await page.addInitScript(({ environmentId, workspaceId }) => {
    window.__SPINE_RUNTIME__ = {
      authentication: {
        status: 'authenticated',
        role: 'administrator',
        identityVersion: 'container-fixture-principal-v1',
        getBearerCredential: () => 'container-fixture-token',
      },
      scope: {
        defaultEnvironmentId: environmentId,
        defaultWorkspaceId: workspaceId,
        environments: [{ id: environmentId, label: 'Local' }],
        workspaces: [{ id: workspaceId, label: 'Основная' }],
      },
    };
  }, { environmentId: localEnvironment, workspaceId: primaryWorkspace });
}

test('test_controlled_fixture_shows_embedded_data_without_test_route_interception', { tag: '@embedded-fixture' }, async ({ page }) => {
  await installContainerRuntime(page);
  await page.goto('/knowledge/documents');

  await expect(page.getByTestId('spine-control-plane')).toHaveAttribute('data-presentation-runtime', 'halaska');
  await expect(page.getByRole('table', { name: 'Документы текущей рабочей области' })).toBeVisible();
  await expect(page.locator('tbody tr')).toHaveCount(14);
  await expect(page.getByText('sd/operations').first()).toBeVisible();
});

test('test_chat_fixture_shows_embedded_sessions_and_messages', { tag: '@embedded-fixture' }, async ({ page }) => {
  await installContainerRuntime(page);
  await page.goto('/chat');

  await expect(page.getByTestId('spine-control-plane')).toHaveAttribute('data-presentation-runtime', 'halaska');
  const sessions = page.getByRole('list', { name: 'Сессии вопросов и ответов' }).getByRole('listitem');
  await expect(sessions).toHaveCount(3);
  await sessions.first().click();
  await expect(page.getByRole('article')).toHaveCount(5);
  await expect(page.getByRole('article').first().getByText('Какой срок хранения исходных файлов и что происходит с историей при удалении?')).toBeVisible();
  await expect(page.getByRole('article').getByText('Когда релиз проекции?')).toBeVisible();
  await expect(page.getByRole('article').getByText('Источники расходятся.')).toBeVisible();
  const answer = page.getByText('Исходные файлы хранятся 36 месяцев.');
  await expect(answer).toBeVisible();
  expect((await page.getByRole('article').first().boundingBox())?.width).toBeGreaterThan(300);

  await page.getByRole('button', { name: /Открыть источник 1/ }).first().click();
  await expect(page.getByText('Срок хранения исходных файлов — 36 месяцев; логическое удаление не затрагивает историю ревизий.')).toBeVisible();

  await sessions.nth(1).click();
  await expect(page.getByRole('article')).toHaveCount(5);
  await expect(page.getByText('Недостаточно данных для ответа')).toBeVisible();
  await expect(page.getByText('Доступ запрещён')).toBeVisible();
  await expect(page.getByText('Завершилось с ошибкой')).toBeVisible();
  await expect(page.getByText('Отменён')).toBeVisible();
});

test('test_requirements_artifact_is_restored_from_scoped_api_after_browser_reload', async ({ page }) => {
  const artifactRequests: { authorization?: string; workspace?: string; environment?: string }[] = [];
  await installContainerRuntime(page);
  await page.route('**/api/v1/runs/' + workflowRun + '**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/requirements')) {
      const headers = route.request().headers();
      artifactRequests.push({
        authorization: headers.authorization,
        workspace: headers['x-workspace-id'],
        environment: headers['x-environment-id'],
      });
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          artifact_id: requirementsArtifact,
          artifact_version: 1,
          schema_version: 'requirements:v1',
          production_identity: 'requirements-extraction-v1',
          production_digest: 'a'.repeat(64),
          workspace_id: primaryWorkspace,
          environment_id: localEnvironment,
          work_item_id: 'b0000000-0000-4000-8000-000000000003',
          run_id: workflowRun,
          state: 'partial',
          upstream_artifact_ids: [],
          predecessor_version: null,
          requirements: [{
            requirement_id: 'b0000000-0000-4000-8000-000000000004',
            category: 'functional',
            statement: 'Deliver the product proposal.',
            constraints: [],
            uncertainty: 'The schedule needs clarification.',
            ambiguous_markers: ['schedule'],
            missing_markers: ['delivery_date'],
            state: 'partial',
            review_state: 'needs_review',
            confidence: null,
            source_refs: [],
            evidence_refs: [],
            evidence_status: 'unverified',
            upstream_artifact_ids: [],
          }],
        }),
      });
      return;
    }
    if (url.pathname.endsWith('/events')) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ schema_version: 1, items: [], limit: 100, next_cursor: null }),
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 1,
        run_id: workflowRun,
        work_item_id: 'b0000000-0000-4000-8000-000000000003',
        workspace_id: primaryWorkspace,
        environment_id: localEnvironment,
        workflow_key: 'commercial.requirements',
        workflow_version_id: 'b0000000-0000-4000-8000-000000000005',
        workflow_version: '1.0.0',
        run_version: 1,
        state: 'accepted',
        allowed_commands: [],
        created_at: '2026-10-29T00:00:00Z',
      }),
    });
  });

  await page.goto(`/workflow/runs/${workflowRun}`);
  await expect(page.getByRole('heading', { name: 'Требования · версия 1' })).toBeVisible();
  await expect(page.getByText('Deliver the product proposal.')).toBeVisible();
  await expect(page.getByText('Не указано: delivery_date')).toBeVisible();
  await expect(page.getByText('Источники не проверены')).toBeVisible();
  expect(artifactRequests).toHaveLength(1);

  await page.reload();

  await expect(page.getByRole('heading', { name: 'Требования · версия 1' })).toBeVisible();
  await expect(page.getByText('Deliver the product proposal.')).toBeVisible();
  expect(artifactRequests).toHaveLength(2);
  expect(artifactRequests.every(({ authorization, workspace, environment }) =>
    authorization === 'Bearer container-fixture-token'
      && workspace === primaryWorkspace
      && environment === localEnvironment,
  )).toBe(true);
});

test('test_qa_runs_fixture_shows_embedded_run_history', { tag: '@embedded-fixture' }, async ({ page }) => {
  await installContainerRuntime(page);
  await page.goto('/qa/runs');

  await expect(page.getByTestId('spine-control-plane')).toHaveAttribute('data-presentation-runtime', 'halaska');
  await expect(page.getByRole('table', { name: 'Запуски Q&A текущей рабочей области' })).toBeVisible();
  await expect(page.locator('tbody tr')).toHaveCount(11);
  await page.locator('tbody tr').first().click();
  await expect(page.getByTestId('spine-control-plane')).toHaveAttribute('data-presentation-runtime', 'halaska');
  await expect(page.getByRole('heading', { level: 2, name: 'Детали запуска Q&A' })).toBeVisible();
});

test('test_fixture_query_uses_the_refreshed_control_plane_interface', async ({ page }) => {
  await installControlledDocuments(page);

  await page.goto('/knowledge/documents?fixture=controlled');

  await expect(page.getByRole('navigation', { name: 'Разделы workspace' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Знания', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('table', { name: 'Документы текущей рабочей области' })).toBeVisible();
  await expect(page.getByText('Control Plane Spine')).toBeVisible();
});

test('test_live_ported_interface_does_not_fall_back_to_demo_documents', async ({ page }) => {
  await page.addInitScript(({ environmentId, workspaceId }) => {
    window.__SPINE_RUNTIME__ = {
      authentication: {
        status: 'authenticated',
        role: 'administrator',
        identityVersion: 'live-empty-principal-v1',
        getBearerCredential: () => 'live-empty-token',
      },
      scope: {
        defaultEnvironmentId: environmentId,
        defaultWorkspaceId: workspaceId,
        environments: [{ id: environmentId, label: 'Local' }],
        workspaces: [{ id: workspaceId, label: 'Основная' }],
      },
    };
  }, { environmentId: localEnvironment, workspaceId: primaryWorkspace });
  await page.route('**/api/v1/documents*', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(controlledDocumentCollection(route.request().headers(), [])),
      status: 200,
    });
  });

  await page.goto('/knowledge/documents');

  await expect(page.getByText('Пока нет данных')).toBeVisible();
  await expect(page.getByText('Регламент описывает порядок приёма')).toHaveCount(0);
  await expect(page.locator('tbody tr')).toHaveCount(0);
});

test('test_refreshed_knowledge_ui_reads_backend_with_runtime_auth_and_scope', async ({ page }) => {
  const documentRequests = await installControlledDocuments(page);
  await page.addInitScript(({ environmentId, workspaceId }) => {
    window.__SPINE_RUNTIME__ = {
      authentication: {
        status: 'authenticated',
        role: 'administrator',
        identityVersion: 'ported-ui-principal-v1',
        getBearerCredential: () => 'ported-ui-token',
      },
      scope: {
        defaultEnvironmentId: environmentId,
        defaultWorkspaceId: workspaceId,
        environments: [{ id: environmentId, label: 'Local' }],
        workspaces: [{ id: workspaceId, label: 'Основная' }],
      },
    };
  }, { environmentId: localEnvironment, workspaceId: primaryWorkspace });

  await page.goto('/knowledge/documents');

  await expect(page.getByRole('table', { name: 'Документы текущей рабочей области' })).toBeVisible();
  await expect(page.getByRole('row', { name: new RegExp(primaryDocument) })).toBeVisible();
  expect(documentRequests).toHaveLength(1);
  expect(documentRequests[0]?.headers.authorization).toBe('Bearer ported-ui-token');
  expect(documentRequests[0]?.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(documentRequests[0]?.headers['x-environment-id']).toBe(localEnvironment);
});

test('test_refreshed_chat_ui_reads_backend_sessions_and_messages', async ({ page }) => {
  await installChatWorkspaceRoutes(page);
  await page.addInitScript(({ environmentId, workspaceId }) => {
    window.__SPINE_RUNTIME__ = {
      authentication: {
        status: 'authenticated',
        role: 'administrator',
        identityVersion: 'ported-chat-principal-v1',
        getBearerCredential: () => 'ported-chat-token',
      },
      scope: {
        defaultEnvironmentId: environmentId,
        defaultWorkspaceId: workspaceId,
        environments: [{ id: environmentId, label: 'Staging' }],
        workspaces: [{ id: workspaceId, label: 'Продажи' }],
      },
    };
  }, { environmentId: stagingEnvironment, workspaceId: salesWorkspace });

  await page.goto('/chat');

  const sessions = page.getByRole('list', { name: 'Сессии вопросов и ответов' }).getByRole('listitem');
  await expect(sessions).toHaveCount(2);
  await sessions.first().click();
  await expect(page.getByText('Достаточно ли оснований для ответа?')).toBeVisible();
  await expect(page.getByText('Недостаточно данных для ответа')).toBeVisible();
});

test('test_control_plane_navigation_preserves_scope_and_generated_client_context', async ({ page }) => {
  const healthRequests = await installControlledHealth(page);
  const documentRequests = await installControlledDocuments(page);

  await page.goto('/?fixture=controlled');
  await expect(page).toHaveTitle('Знания · Control Plane Spine');
  await expect(page.getByText('Control Plane Spine')).toBeVisible();
  await page.getByRole('button', { name: 'Проверить соединение' }).click();
  await expect(page.getByRole('status')).toContainText('Соединение проверено');

  await choose(page, 'Рабочая область', 'Продажи');
  await choose(page, 'Среда', 'Staging');
  await expect(page).toHaveURL(
    new RegExp(`workspace=${salesWorkspace}&environment=${stagingEnvironment}`),
  );

  await page.getByRole('button', { name: 'Знания', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/knowledge/documents\\?`));
  await expect(page.getByRole('heading', { name: 'Знания', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /Рабочая область: Продажи, Среда: Staging/ })).toBeVisible();

  await page.reload();
  await expect(page.getByRole('button', { name: /Рабочая область: Продажи, Среда: Staging/ })).toBeVisible();

  await page.getByRole('button', { name: 'Проверить соединение' }).click();
  await expect(page.getByRole('status').first()).toContainText('Соединение недоступно');
  expect(healthRequests).toHaveLength(2);
  expect(healthRequests[0]?.headers.authorization).toBe('Bearer controlled-browser-token');
  expect(healthRequests[0]?.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(healthRequests[0]?.headers['x-environment-id']).toBe(localEnvironment);
  expect(healthRequests[1]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(healthRequests[1]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(documentRequests.some(({ headers }) =>
    headers['x-workspace-id'] === salesWorkspace && headers['x-environment-id'] === stagingEnvironment,
  )).toBe(true);
  expect(healthRequests.every(({ url }) => !url.includes('controlled-browser-token'))).toBe(true);
  await expect(page.locator('body')).not.toContainText(/customer|protected|resource_exists/i);
});

test('test_shared_shell_keeps_navigation_and_scope_controls_usable_on_mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?fixture=controlled');

  await expect(page.getByRole('navigation', { name: 'Разделы workspace' })).toBeVisible();
  await expect(page.getByRole('button', { name: /Рабочая область:/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await page.getByRole('button', { name: 'Чат', exact: true }).click();
  await expect(page).toHaveURL(/\/chat\?fixture=controlled$/);
  await expect(page.getByRole('heading', { level: 1, name: 'Чат' })).toBeVisible();
});

test('test_qa_runs_renders_valid_empty_collection_without_service_error', async ({ page }) => {
  const requests: { authorization?: string; workspace?: string; environment?: string }[] = [];
  await page.route('**/api/v1/qa/runs**', async (route) => {
    const headers = route.request().headers();
    const workspace = headers['x-workspace-id'];
    const environment = headers['x-environment-id'];
    if (!workspace || !environment) throw new Error('Q&A run request is missing scope headers');
    requests.push({
      authorization: headers.authorization,
      workspace,
      environment,
    });
    await route.fulfill({
      contentType: 'application/json',
      status: 200,
      body: JSON.stringify({
        schema_version: 1,
        workspace_id: workspace,
        environment_id: environment,
        trace_id: traceId,
        limit: 25,
        next_cursor: null,
        items: [],
      }),
    });
  });

  await page.goto('/qa/runs?fixture=controlled');

  await expect(page.getByRole('heading', { level: 1, name: 'Запуски Q&A' })).toBeVisible();
  await expect(page.getByText('Пока нет данных')).toBeVisible();
  await expect(page.getByText('В этом разделе ещё ничего нет.')).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(requests).toHaveLength(1);
  expect(requests[0]?.authorization).toBe('Bearer controlled-browser-token');
  expect(requests[0]?.workspace).toMatch(/^[0-9a-f-]{36}$/);
  expect(requests[0]?.environment).toMatch(/^[0-9a-f-]{36}$/);
});

test('test_documents_discovery_filters_paginates_and_navigates_without_content_identity', async ({ page }) => {
  const documentRequests = await installControlledDocuments(page);
  await page.goto(
    `/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );

  await expect(page.getByRole('heading', { level: 1, name: 'Знания', exact: true })).toBeVisible();
  await expect(page.getByRole('table')).toBeVisible();
  await expect(page.getByRole('row', { name: `Документ ${firstDocument}` })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Доступ и безопасные метаданные' })).toBeVisible();
  await expect(page.getByText('security-domain:controlled')).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Доступ и безопасные метаданные' })).toContainText('v2 · gen 3');
  await expect(page.locator('body')).not.toContainText(/secret\.pdf|protected title|checksum/i);

  await page.getByRole('button', { name: 'Фильтры' }).click();
  await page.getByRole('combobox', { name: 'Публикация' }).click();
  await page.getByRole('option', { name: 'Опубликован', exact: true }).click();
  await expect(page).toHaveURL(/publication_status=published/);
  await page.getByRole('button', { name: 'Далее' }).click();
  await expect(page).toHaveURL(/cursor=documents-next/);
  await expect(page.getByRole('row', { name: `Документ ${secondDocument}` })).toBeVisible();

  await page.getByRole('row', { name: `Документ ${secondDocument}` }).click();
  await expect(page).toHaveURL(
    new RegExp(`/knowledge/documents/${secondDocument}\\?.*workspace=${salesWorkspace}.*publication_status=published.*cursor=documents-next`),
  );
  await expect(page.getByText('Операционный обзор документа', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть панель' }).click();
  await expect(page.getByRole('row', { name: `Документ ${secondDocument}` })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('row', { name: `Документ ${secondDocument}` })).toBeVisible();

  const restoredScopeRequest = page.waitForRequest((request) => {
    const headers = request.headers();
    return request.url().includes('/api/v1/documents')
      && headers['x-workspace-id'] === primaryWorkspace
      && headers['x-environment-id'] === localEnvironment;
  });
  await choose(page, 'Рабочая область', 'Основная');
  await choose(page, 'Среда', 'Local');
  await expect(page).toHaveURL(new RegExp(`workspace=${primaryWorkspace}&environment=${localEnvironment}`));
  await restoredScopeRequest;
  await expect(page.getByRole('row', { name: `Документ ${primaryDocument}` })).toBeVisible();
  await expect(page.getByRole('row', { name: `Документ ${firstDocument}` })).toHaveCount(0);

  expect(documentRequests.length).toBeGreaterThanOrEqual(2);
  expect(documentRequests.every(({ headers }) => headers.authorization === 'Bearer controlled-browser-token')).toBe(true);
  expect(documentRequests.some(({ headers }) => headers['x-workspace-id'] === salesWorkspace && headers['x-environment-id'] === stagingEnvironment)).toBe(true);
  expect(documentRequests.some(({ headers }) => headers['x-workspace-id'] === primaryWorkspace && headers['x-environment-id'] === localEnvironment)).toBe(true);
  expect(documentRequests.every(({ url }) => !url.includes('unsupported'))).toBe(true);
});

test('test_documents_upload_reviews_batch_and_polls_processing', async ({ page }) => {
  await installControlledDocuments(page);
  const uploadRequests = await installControlledBatchUpload(page);
  await installControlledRunReadModels(page);

  await page.goto(
    `/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );
  await page.getByRole('button', { name: 'Загрузить документы' }).click();
  await page.locator('[aria-label="Перетащите файлы сюда"]').evaluate((element) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File(['synthetic upload fixture'], 'proposal.txt', { type: 'text/plain' }));
    dataTransfer.items.add(new File(['synthetic markdown fixture'], 'context.md', { type: 'text/markdown' }));
    element.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer }));
  });

  await expect(page.getByRole('region', { name: 'Проверка перед отправкой' })).toBeVisible();
  await expect(page.getByText('Файлов в очереди: 2')).toBeVisible();
  await page.getByRole('dialog', { name: 'Загрузить документы' })
    .getByRole('button', { name: 'Загрузить документы' }).click();
  await expect(page.getByText(/Запрос принят\. Разбор и индексация ещё не завершены/)).toBeVisible();

  expect(uploadRequests).toHaveLength(1);
  expect(uploadRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(uploadRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(uploadRequests[0]?.headers['idempotency-key']).toMatch(/^batch-upload-/);
  expect(uploadRequests[0]?.body).toContain('proposal.txt');
  expect(uploadRequests[0]?.body).toContain('context.md');
});

test('test_documents_upload_accepts_docx_and_displays_completed_ingestion_status', async ({ page }) => {
  await installControlledDocuments(page);
  const uploadRequests = await installControlledBatchUpload(page);

  await page.goto(
    `/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );
  await page.getByRole('button', { name: 'Загрузить документы' }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: 'handbook.docx',
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: Buffer.from('synthetic docx fixture'),
  });

  await expect(page.getByRole('region', { name: 'Проверка перед отправкой' })).toBeVisible();
  await expect(page.getByText('handbook.docx')).toBeVisible();
  await page.getByRole('dialog', { name: 'Загрузить документы' })
    .getByRole('button', { name: 'Загрузить документы' }).click();
  await expect(page.getByText(/Запрос принят\. Разбор и индексация ещё не завершены/)).toBeVisible();

  expect(uploadRequests).toHaveLength(1);
  expect(uploadRequests[0]?.body).toContain('handbook.docx');
  expect(uploadRequests[0]?.body).toContain(
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  );
});

test('test_ingestion_run_displays_partial_outcomes_retryability_and_safe_diagnostics', async ({ page }) => {
  await installControlledRunReadModels(page);
  const runRequests: Array<Record<string, string>> = [];
  await page.route(`**/api/v1/ingestion/runs/${firstDocument}`, async (route) => {
    const headers = route.request().headers();
    runRequests.push(headers);
    if (!isOwnedScope(headers, salesWorkspace, stagingEnvironment)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
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

  const isolatedRunRequest = page.waitForRequest((request) => {
    const headers = request.headers();
    return request.url().endsWith(`/api/v1/ingestion/runs/${firstDocument}`)
      && headers['x-workspace-id'] === primaryWorkspace
      && headers['x-environment-id'] === localEnvironment;
  });
  await choose(page, 'Рабочая область', 'Основная');
  await choose(page, 'Среда', 'Local');
  await isolatedRunRequest;
  await expect(page).toHaveURL(new RegExp(`workspace=${primaryWorkspace}.*environment=${localEnvironment}`));
  await page.reload();
  await expect(page.getByRole('alert', { name: 'Доступ запрещён' })).toBeVisible();
  await expect(page.getByText('Ошибка: parser.invalid (parse)')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('failed.txt');
  expect(runRequests.some((headers) => headers['x-workspace-id'] === salesWorkspace && headers['x-environment-id'] === stagingEnvironment)).toBe(true);
  expect(runRequests.some((headers) => headers['x-workspace-id'] === primaryWorkspace && headers['x-environment-id'] === localEnvironment)).toBe(true);
});

test('test_document_retry_polls_durable_operation_and_reloads_its_run', async ({ page }) => {
  await installControlledRunReadModels(page);
  const operationId = '80000000-0000-4000-8000-000000000001';
  const retryRunId = '80000000-0000-4000-8000-000000000002';
  const retryItemId = '80000000-0000-4000-8000-000000000003';
  const retrySourceObjectId = '80000000-0000-4000-8000-000000000004';
  const retryRevisionId = '80000000-0000-4000-8000-000000000005';
  const failureId = '60000000-0000-0000-0000-000000000002';
  const commandRequests: Array<{ body: string; headers: Record<string, string>; url: string }> = [];
  let commandReady = false;
  await page.route(`**/api/v1/ingestion/runs/${firstDocument}`, async (route) => {
    const status = controlledPartialRunStatus();
    const failed = status.items[1]!;
    failed.source_object_id = retrySourceObjectId;
    failed.source_revision_id = retryRevisionId;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(status), status: 200 });
  });
  await page.route('**/api/v1/documents/processing/retry', async (route) => {
    const request = route.request();
    commandRequests.push({ body: request.postData() ?? '', headers: request.headers(), url: request.url() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(processingCommandReceipt({
      failureIds: [], itemId: retryItemId, operationId, runId: retryRunId, sourceObjectId: retrySourceObjectId,
      sourceRevisionId: retryRevisionId, status: 'accepted',
    })), status: 202 });
  });
  await page.route(`**/api/v1/documents/processing/operations/${operationId}`, async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(processingCommandReceipt({
      failureIds: [], itemId: retryItemId, operationId, runId: retryRunId, sourceObjectId: retrySourceObjectId,
      sourceRevisionId: retryRevisionId, status: commandReady ? 'ready' : 'accepted',
    })), status: 200 });
  });
  await page.route(`**/api/v1/ingestion/runs/${retryRunId}`, async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
      items: [controlledUploadItem({
        input_identity: 'failed.txt', item_id: retryItemId, input_ordinal: 1, source_object_id: retrySourceObjectId,
        source_revision_id: retryRevisionId, item_state: 'processed', processing: 'succeeded', status: 'succeeded',
        terminal: true,
      })],
      run_id: retryRunId, run_state: 'completed', schema_version: 1, status: 'succeeded', terminal: true,
    }), status: 200 });
  });

  await page.goto(`/runs/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  const failedFile = page.getByRole('listitem').filter({ hasText: 'failed.txt' });
  await failedFile.getByRole('button', { name: 'Повторить обработку файла' }).click();
  await expect(failedFile).toContainText('Команда в очереди. Обработка ещё не завершена.');
  commandReady = true;
  await page.reload();
  const reloadedFailedFile = page.getByRole('listitem').filter({ hasText: 'failed.txt' });
  await expect(reloadedFailedFile.getByRole('region', { name: 'Результат команды обработки' })).toContainText('Обработка завершена');
  await expect(reloadedFailedFile).toContainText('Ошибка');
  await expect(page.locator('body')).not.toContainText(/provider|stack|customer secret/i);
  await expect(reloadedFailedFile.getByRole('link', { name: 'Открыть запуск обработки' })).toHaveAttribute(
    'href', new RegExp(`/runs/${retryRunId}.*workspace=${salesWorkspace}.*environment=${stagingEnvironment}`),
  );

  expect(commandRequests).toHaveLength(1);
  expect(commandRequests[0]?.url).toContain('/api/v1/documents/processing/retry');
  expect(commandRequests[0]?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);
  expect(commandRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(commandRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(JSON.parse(commandRequests[0]!.body)).toEqual({
    expected_current_revision_id: retryRevisionId,
    parent_failure_id: failureId,
    parent_processing_receipt_id: '60000000-0000-4000-8000-000000000003',
    source_object_id: retrySourceObjectId,
    source_revision_id: retryRevisionId,
  });

  await reloadedFailedFile.getByRole('link', { name: 'Открыть запуск обработки' }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/${retryRunId}.*workspace=${salesWorkspace}`));
  await page.reload();
  await expect(page.getByRole('list', { name: 'Результаты файлов' })).toContainText('Готов');
});

test('test_document_reprocess_uses_current_revision_and_conflict_requires_refresh', async ({ page }) => {
  const requests: Array<{ body: string; headers: Record<string, string> }> = [];
  let returnReady = false;
  await installControlledDocumentDetail(page);
  await page.route('**/api/v1/documents/processing/reprocess', async (route) => {
    const request = route.request();
    requests.push({ body: request.postData() ?? '', headers: request.headers() });
    if (returnReady) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(processingCommandReceipt({
          failureIds: [], itemId: '80000000-0000-4000-8000-000000000003',
          operationId: '80000000-0000-4000-8000-000000000001',
          runId: '80000000-0000-4000-8000-000000000002',
          sourceObjectId: firstDocument, sourceRevisionId: secondDocument, status: 'ready',
          triggerKind: 'reprocess',
        })),
        status: 202,
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        code: 'spine.processing.revision_conflict',
        retryability: 'never',
        safe_message: 'The selected revision is no longer current.',
        schema_version: 1,
        trace_id: traceId,
        protected_detail: 'provider failure secret',
      }),
      status: 409,
    });
  });

  await page.goto(`/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}&tab=revisions&revision=${secondDocument}`);
  await page.getByRole('button', { name: 'Обработать текущую ревизию заново' }).click();
  const conflictStatus = page.getByRole('status').filter({ hasText: 'Команда конфликтует с текущим состоянием' });
  await expect(conflictStatus).toContainText('Команда конфликтует с текущим состоянием');
  await expect(conflictStatus).toContainText('Обновите состояние проекции и повторите запрос.');
  await expect(page.locator('body')).not.toContainText('provider failure secret');
  expect(requests).toHaveLength(1);

  returnReady = true;
  await page.getByRole('button', { name: 'Обработать текущую ревизию заново' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Команда в очереди' })).toBeVisible();
  expect(requests).toHaveLength(2);
  expect(requests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(requests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(JSON.parse(requests[0]!.body)).toMatchObject({
    expected_current_revision_id: secondDocument,
    processing_profile_reference: 'document-processing:r1',
    source_object_id: firstDocument,
    source_revision_id: secondDocument,
  });
});

test('test_document_retry_replay_failed_cancelled_and_denied_states_are_safe', async ({ page }) => {
  await installControlledRunReadModels(page);
  const sourceObjectId = '80000000-0000-4000-8000-000000000004';
  const sourceRevisionId = '80000000-0000-4000-8000-000000000005';
  let commandCount = 0;
  await page.route(`**/api/v1/ingestion/runs/${firstDocument}`, async (route) => {
    const status = controlledPartialRunStatus();
    const failed = status.items[1]!;
    failed.source_object_id = sourceObjectId;
    failed.source_revision_id = sourceRevisionId;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(status), status: 200 });
  });
  await page.route('**/api/v1/documents/processing/retry', async (route) => {
    commandCount += 1;
    if (commandCount === 4) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'spine.authorization.denied',
          retryability: 'never',
          safe_message: 'Доступ запрещён',
          schema_version: 1,
          trace_id: traceId,
          protected_detail: 'provider failure secret',
        }),
        status: 403,
      });
      return;
    }
    const status = commandCount === 1 ? 'ready' : commandCount === 2 ? 'failed' : 'cancelled';
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(processingCommandReceipt({
        failureIds: [], itemId: '80000000-0000-4000-8000-000000000003',
        operationId: `80000000-0000-4000-8000-00000000000${commandCount}`,
        runId: '80000000-0000-4000-8000-000000000002', sourceObjectId, sourceRevisionId,
        status, replay: commandCount === 1,
      })),
      status: 202,
    });
  });

  await page.goto(`/runs/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  const failedFile = page.getByRole('listitem').filter({ hasText: 'failed.txt' });
  const retry = failedFile.getByRole('button', { name: 'Повторить обработку файла' });
  await retry.click();
  await expect(failedFile.getByRole('region', { name: 'Результат команды обработки' })).toContainText('Обработка завершена');
  await expect(failedFile).toContainText('Показан сохранённый результат этой команды.');

  await retry.click();
  await expect(failedFile.getByRole('region', { name: 'Результат команды обработки' })).toContainText('Ошибка обработки');
  await retry.click();
  await expect(failedFile.getByRole('region', { name: 'Результат команды обработки' })).toContainText('Обработка отменена');
  await retry.click();
  await expect(failedFile.getByRole('alert')).toContainText('Доступ запрещён');
  await expect(page.locator('body')).not.toContainText('provider failure secret');
  await expect(failedFile.getByRole('button', { name: 'Повторить ту же команду' })).toHaveCount(0);
  expect(commandCount).toBe(4);
});

test('test_document_retry_reuses_idempotency_key_and_returns_stable_replay_receipt', async ({ page }) => {
  await installControlledRunReadModels(page);
  const sourceObjectId = '80000000-0000-4000-8000-000000000004';
  const sourceRevisionId = '80000000-0000-4000-8000-000000000005';
  const idempotencyKeys: string[] = [];
  let attempt = 0;
  await page.route(`**/api/v1/ingestion/runs/${firstDocument}`, async (route) => {
    const status = controlledPartialRunStatus();
    status.items[1]!.source_object_id = sourceObjectId;
    status.items[1]!.source_revision_id = sourceRevisionId;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(status), status: 200 });
  });
  await page.route('**/api/v1/documents/processing/retry', async (route) => {
    attempt += 1;
    idempotencyKeys.push(route.request().headers()['idempotency-key'] ?? '');
    if (attempt === 1) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ code: 'spine.processing.retryable', retryability: 'same_command', safe_message: 'Retry the same command.', schema_version: 1, trace_id: traceId }),
        status: 503,
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(processingCommandReceipt({
        failureIds: [],
        itemId: '80000000-0000-4000-8000-000000000003',
        operationId: '80000000-0000-4000-8000-000000000009',
        replay: true,
        runId: '80000000-0000-4000-8000-000000000002',
        sourceObjectId,
        sourceRevisionId,
        status: 'ready',
      })),
      status: 202,
    });
  });

  await page.goto(`/runs/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  const failedFile = page.getByRole('listitem').filter({ hasText: 'failed.txt' });
  await failedFile.getByRole('button', { name: 'Повторить обработку файла' }).click();
  await failedFile.getByRole('button', { name: 'Повторить ту же команду' }).click();

  const receipt = failedFile.getByRole('region', { name: 'Результат команды обработки' });
  await expect(receipt).toContainText('80000000-0000-4000-8000-000000000009');
  await expect(receipt).toContainText('Показан сохранённый результат этой команды.');
  expect(idempotencyKeys).toHaveLength(2);
  expect(idempotencyKeys[0]).toMatch(/^[0-9a-f-]{36}$/);
  expect(idempotencyKeys[1]).toBe(idempotencyKeys[0]);
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
  await page.getByRole('button', { name: 'Загрузить документы' }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: 'unsupported.exe',
    mimeType: 'application/x-msdownload',
    buffer: Buffer.from('synthetic invalid fixture'),
  });

  await expect(page.getByRole('alert')).toContainText(
    'Используйте PDF, DOCX, XLSX, Markdown или TXT',
  );
  await expect(page.getByRole('alert')).not.toContainText('unsupported.exe');
  expect(uploadRequests).toHaveLength(0);
});

test('test_document_detail_deep_link_reloads_and_preserves_scope_and_subview_navigation', async ({ page }) => {
  const documentRequests = await installControlledDocumentDetail(page);
  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );

  await expect(page.getByText('Операционный обзор документа')).toBeVisible();
  await expect(page.getByText('Доступ и безопасные метаданные')).toBeVisible();
  await expect(page.getByText('security-domain:controlled')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Скопировать: Идентификатор источника' })).toBeVisible();
  await expect(page.locator('body')).not.toContainText(/secret\.pdf|protected title|checksum/i);

  await page.getByRole('tab', { name: 'История ревизий' }).click();
  await expect(page).toHaveURL(/tab=revisions/);
  await expect(page.getByRole('tab', { name: 'История ревизий', selected: true })).toBeVisible();
  await expect(page.getByText('30000000…0002')).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/tab=revisions/);
  await expect(page.getByRole('tab', { name: 'История ревизий', selected: true })).toBeVisible();

  const isolatedDetailRequest = page.waitForRequest((request) => {
    const headers = request.headers();
    return request.url().endsWith(`/api/v1/documents/${firstDocument}`)
      && headers['x-workspace-id'] === primaryWorkspace
      && headers['x-environment-id'] === localEnvironment;
  });
  await choose(page, 'Рабочая область', 'Основная');
  await choose(page, 'Среда', 'Local');
  await isolatedDetailRequest;
  await expect(page).toHaveURL(new RegExp(`workspace=${primaryWorkspace}.*environment=${localEnvironment}.*tab=revisions`));
  await expect(page.getByRole('alert')).toContainText('Нет доступа к данным');
  await expect(page.getByText('security-domain:controlled')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText(/secret\.pdf|protected title|checksum/i);
  await page.reload();
  await expect(page.getByRole('alert')).toContainText('Нет доступа к данным');

  expect(documentRequests.length).toBeGreaterThanOrEqual(7);
  expect(documentRequests.every(({ headers }) => headers.authorization === 'Bearer controlled-browser-token')).toBe(true);
  expect(documentRequests.some(({ headers }) => headers['x-workspace-id'] === salesWorkspace && headers['x-environment-id'] === stagingEnvironment)).toBe(true);
  expect(documentRequests.some(({ headers }) => headers['x-workspace-id'] === primaryWorkspace && headers['x-environment-id'] === localEnvironment)).toBe(true);
});

test('test_document_lifecycle_actions_are_scope_owned_and_preserve_tombstone_lineage', async ({ page }) => {
  await installControlledDocumentDetail(page);
  const replacementUploadRequests: Array<{ body: Buffer | null; headers: Record<string, string>; method: string; url: string }> = [];
  const lifecycleRequests: Array<{ body: string; headers: Record<string, string>; method: string; url: string }> = [];
  const statusRequests: Array<{ headers: Record<string, string>; url: string }> = [];
  const statusRequestCounts = new Map<string, number>();
  await page.route(`**/api/v1/documents/${primaryDocument}/revisions*`, async (route) => {
    const headers = route.request().headers();
    if (!isOwnedScope(headers, primaryWorkspace, localEnvironment)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
    const revisions = [
      controlledRevision({
        kind: 'tombstone',
        lifecycle: 'tombstoned',
        observedAt: '2026-01-02T06:00:00Z',
        predecessorRevisionId: '41000000-0000-4000-8000-000000000012',
        sourceRevisionId: '41000000-0000-4000-8000-000000000013',
      }),
      controlledRevision({
        lifecycle: 'superseded',
        observedAt: '2026-01-02T05:00:00Z',
        predecessorRevisionId: primaryRevision,
        sourceRevisionId: '41000000-0000-4000-8000-000000000012',
        supersededByRevisionId: '41000000-0000-4000-8000-000000000013',
      }),
      controlledRevision({
        lifecycle: 'superseded',
        sourceRevisionId: primaryRevision,
        supersededByRevisionId: '41000000-0000-4000-8000-000000000012',
      }),
    ];
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
      environment_id: headers['x-environment-id'], items: revisions, limit: 25, next_cursor: null,
      schema_version: 1, trace_id: traceId, workspace_id: headers['x-workspace-id'],
    }), status: 200 });
  });
  await page.route('**/api/v1/documents/lifecycle/operations/*', async (route) => {
    const request = route.request();
    statusRequests.push({ headers: request.headers(), url: request.url() });
    const sourceObjectId = request.url().endsWith(`/${primaryDocument}`) ? primaryDocument : firstDocument;
    const operationKind = sourceObjectId === primaryDocument ? 'tombstone' : 'replacement';
    const requestCount = (statusRequestCounts.get(sourceObjectId) ?? 0) + 1;
    statusRequestCounts.set(sourceObjectId, requestCount);
    const status = requestCount % 2 === 1 ? 'processing_dispatched' : 'completed';
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(controlledLifecycleReceipt(operationKind, status, sourceObjectId, sourceObjectId === primaryDocument ? primaryRevision : secondDocument)),
      status: 200,
    });
  });
  await page.route(`**/api/v1/documents/${firstDocument}/replacement-upload`, async (route) => {
    const request = route.request();
    if (!isOwnedScope(request.headers(), salesWorkspace, stagingEnvironment)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
    replacementUploadRequests.push({ body: request.postDataBuffer(), headers: request.headers(), method: request.method(), url: request.url() });
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        acceptance: 'accepted',
        binding_id: firstDocument,
        failure_ids: [],
        item_id: secondDocument,
        processing: 'succeeded',
        warning_ids: [],
        replay: false,
        run_id: firstDocument,
        schema_version: 1,
        source_object_id: firstDocument,
        source_revision_id: secondDocument,
        status: 'succeeded',
        status_url: `/api/v1/ingestion/runs/${firstDocument}/items/${secondDocument}`,
        terminal: true,
      } satisfies components['schemas']['IngestionUploadResponse']),
      status: 202,
    });
  });
  await page.route(`**/api/v1/documents/${primaryDocument}/tombstone`, async (route) => {
    const request = route.request();
    if (!isOwnedScope(request.headers(), primaryWorkspace, localEnvironment)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
    lifecycleRequests.push({ body: request.postData() ?? '', headers: request.headers(), method: request.method(), url: request.url() });
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(controlledLifecycleReceipt('tombstone', 'accepted_processing_pending', primaryDocument, primaryRevision)),
      status: 202,
    });
  });

  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );
  const replacementContent = Buffer.from('# Replacement handbook\n');
  await page.getByRole('button', { name: 'Заменить документ' }).click();
  const replacementDialog = page.getByRole('dialog', { name: 'Заменить содержимое' });
  await replacementDialog.locator('input[type="file"]').setInputFiles({
    name: 'handbook.md',
    mimeType: 'text/markdown',
    buffer: replacementContent,
  });
  await replacementDialog.getByRole('button', { name: 'Заменить документ' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Команда принята сервером' })).toBeVisible();

  const isolatedDetailRequest = page.waitForRequest((request) => {
    const headers = request.headers();
    return request.url().endsWith(`/api/v1/documents/${firstDocument}`)
      && headers['x-workspace-id'] === primaryWorkspace
      && headers['x-environment-id'] === localEnvironment;
  });
  await choose(page, 'Рабочая область', 'Основная');
  await choose(page, 'Среда', 'Local');
  await isolatedDetailRequest;
  await expect(page.getByRole('alert')).toContainText('Нет доступа к данным');
  await expect(page.getByLabel('Причина логического удаления')).toHaveCount(0);
  await page.goto(
    `/knowledge/documents/${primaryDocument}?fixture=controlled&workspace=${primaryWorkspace}&environment=${localEnvironment}`,
  );
  await page.getByRole('button', { name: 'Создать tombstone' }).click();
  const tombstoneDialog = page.getByRole('dialog', { name: 'Логически удалить документ' });
  await tombstoneDialog.getByRole('radio', { name: 'Источник удалён' }).check();
  await tombstoneDialog.getByRole('button', { name: 'Создать tombstone' }).click();
  await expect(page.getByText('Новая неизменяемая tombstone-ревизия сохранена в истории.')).toBeVisible();
  await page.getByRole('tab', { name: 'История ревизий' }).click();
  await expect(page.getByText('Граница удаления')).toBeVisible();
  await expect(page.getByText('41000000…0013')).toBeVisible();
  await expect(page.getByText('41000000…0012')).toBeVisible();
  await expect(page.getByText('30000000…0004')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('tab', { name: 'История ревизий', selected: true })).toBeVisible();

  expect(replacementUploadRequests).toHaveLength(1);
  expect(replacementUploadRequests[0]?.method).toBe('POST');
  expect(replacementUploadRequests[0]?.url).toContain(`/api/v1/documents/${firstDocument}/replacement-upload`);
  expect(replacementUploadRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(replacementUploadRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(replacementUploadRequests[0]?.headers['idempotency-key']).toMatch(/^batch-upload-/);
  expect(replacementUploadRequests[0]?.headers['if-match']).toBe(secondDocument);
  expect(replacementUploadRequests[0]?.headers['x-filename']).toBe('handbook.md');
  expect(replacementUploadRequests[0]?.headers['content-type']).toBe('text/markdown');
  expect(replacementUploadRequests[0]?.body).toEqual(replacementContent);
  expect(lifecycleRequests).toHaveLength(1);
  expect(lifecycleRequests[0]?.headers['idempotency-key']).toMatch(/^document-tombstone-/);
  expect(lifecycleRequests[0]?.body).toContain('source_deleted');
  expect(lifecycleRequests[0]?.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(lifecycleRequests[0]?.headers['x-environment-id']).toBe(localEnvironment);
  expect(lifecycleRequests.every(({ body }) => !body.includes('physical') && !body.includes('canonical_acceptance'))).toBe(true);
  await expect.poll(() => statusRequests.length).toBe(2);
  expect(statusRequests.every(({ headers }) => headers.authorization === 'Bearer controlled-browser-token')).toBe(true);
  expect(statusRequests.every(({ headers }) => headers['x-workspace-id'] === primaryWorkspace && headers['x-environment-id'] === localEnvironment)).toBe(true);
});

test('test_projection_status_scope_switch_reload_and_safe_degradation', async ({ page }) => {
  await installControlledDocumentDetail(page, true);
  const projectionRequests: Array<{ headers: Record<string, string>; url: string }> = [];
  const correlationRequests: Array<{ headers: Record<string, string>; url: string }> = [];
  await page.route('**/api/v1/diagnostics/operational-correlation*', async (route) => {
    const request = route.request();
    const headers = request.headers();
    const url = new URL(request.url());
    correlationRequests.push({ headers, url: request.url() });
    await route.fulfill({
      contentType: 'application/json',
      status: 200,
      body: JSON.stringify({
        authorization_audit_event_id: '70000000-0000-4000-8000-000000000001',
        data: {
          conflict_codes: ['metric_coverage_incomplete'],
          correlation_ids: [],
          coverage: 'incomplete',
          environment_id: stagingEnvironment,
          lineage: [],
          missing_sources: ['delivery_metadata'],
          records: [{
            correlation_id: null,
            deduplication_key: `metric:${projectionRun}`,
            environment_id: stagingEnvironment,
            metric_coverage: 'degraded',
            metric_measurement: 'projection.publication_latency',
            observed_at: '2026-10-17T10:00:00Z',
            run_id: projectionRun,
            schema_version: 'operational-correlation-record:v1',
            source_identity: 'spine.observation:v1',
            source_kind: 'operational_metric',
            source_record_id: '70000000-0000-4000-8000-000000000002',
            source_schema_version: 'operational-metric-event:v1',
            workspace_id: salesWorkspace,
          }],
          reference_id: projectionRun,
          reference_kind: 'run',
          schema_version: 'operational-correlation-view:v1',
          trace_ids: [traceId],
          workspace_id: salesWorkspace,
        },
        environment_id: headers['x-environment-id'],
        schema_version: 1,
        trace_id: traceId,
        workspace_id: headers['x-workspace-id'],
      }),
    });
    expect(url.searchParams.get('reference_kind')).toBe('run');
    expect(url.searchParams.get('reference_id')).toBe(projectionRun);
  });
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
  await panel.getByRole('button', { name: 'Показать диагностику' }).click();
  await expect(page.getByText('Данные корреляции неполные')).toBeVisible();
  await expect(page.getByText('projection.publication_latency')).toBeVisible();
  expect(correlationRequests).toHaveLength(1);
  expect(correlationRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(correlationRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  await expect(page.locator('body')).not.toContainText('provider stack');
  await expect(page.getByText(/не подтверждает доступ к доказательствам/i)).toBeVisible();
  await expect(page.locator('body')).not.toContainText('must not be rendered');
  await expect(page).toHaveURL(/tab=projection.*projection_kind=qa/);

  const isolatedDetailRequest = page.waitForRequest((request) => {
    const headers = request.headers();
    return request.url().endsWith(`/api/v1/documents/${firstDocument}`)
      && headers['x-workspace-id'] === primaryWorkspace
      && headers['x-environment-id'] === localEnvironment;
  });
  await choose(page, 'Рабочая область', 'Основная');
  await choose(page, 'Среда', 'Local');
  await isolatedDetailRequest;
  await expect(page).toHaveURL(new RegExp(`workspace=${primaryWorkspace}.*environment=${localEnvironment}.*tab=projection.*projection_kind=qa`));
  await expect(page.getByRole('alert').filter({ hasText: 'Доступ запрещён' }).first()).toBeVisible();
  const isolatedProjectionRequest = page.waitForRequest((request) => {
    const headers = request.headers();
    return request.url().includes('/api/v1/projections/qa/status')
      && headers['x-workspace-id'] === primaryWorkspace
      && headers['x-environment-id'] === localEnvironment;
  });
  await page.goto(
    `/knowledge/documents/${primaryDocument}?fixture=controlled&workspace=${primaryWorkspace}&environment=${localEnvironment}&tab=projection&projection_kind=qa`,
  );
  await isolatedProjectionRequest;
  await expect(panel.getByText('Недоступен')).toHaveCount(2);
  await page.reload();
  await expect(panel.getByText('Недоступен')).toHaveCount(2);
  expect(projectionRequests.length).toBeGreaterThanOrEqual(3);
  expect(projectionRequests.every(({ url }) => url.includes('/projections/qa/status'))).toBe(true);
  expect(projectionRequests.some(({ headers }) => headers['x-workspace-id'] === salesWorkspace)).toBe(true);
  expect(projectionRequests.some(({ headers }) => headers['x-workspace-id'] === primaryWorkspace)).toBe(true);
  expect(projectionRequests.some(({ headers }) => headers['x-workspace-id'] === primaryWorkspace && headers['x-environment-id'] === localEnvironment)).toBe(true);
});

test('test_projection_recovery_receipt_polls_status_until_server_reports_completion', async ({ page }) => {
  await installControlledDocumentDetail(page);
  let statusReads = 0;
  const commandRequests: Array<{ body: string; idempotencyKey: string; headers: Record<string, string> }> = [];
  await page.route('**/api/v1/projections/qa/status', async (route) => {
    statusReads += 1;
    const headers = route.request().headers();
    const status: components['schemas']['ProjectionStatusResponse'] = statusReads === 1
      ? { projection_kind: 'qa', publication_status: 'published', freshness: 'stale', health: 'healthy', recovery_state: 'not_requested', retryable: false, schema_version: 1 }
      : statusReads === 2
        ? { projection_kind: 'qa', publication_status: 'published', freshness: 'stale', health: 'healthy', recovery_state: 'pending', retryable: false, last_operation: { operation: 'rebuild', status: 'running', retryable: false }, schema_version: 1 }
        : { projection_kind: 'qa', publication_status: 'published', freshness: 'current', health: 'healthy', recovery_state: 'succeeded', retryable: false, last_operation: { operation: 'rebuild', status: 'completed', retryable: false }, schema_version: 1 };
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ data: status, environment_id: headers['x-environment-id'], schema_version: 1, trace_id: traceId, workspace_id: headers['x-workspace-id'] }), status: 200 });
  });
  await page.route('**/api/v1/projections/qa/recovery', async (route) => {
    const request = route.request();
    const headers = request.headers();
    commandRequests.push({ body: request.postData() ?? '', idempotencyKey: headers['idempotency-key'] ?? '', headers });
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ command_id: '70000000-0000-4000-8000-000000000009', environment_id: headers['x-environment-id'], replay: false, result: { action: 'rebuild', command_id: '70000000-0000-4000-8000-000000000009', projection_kind: 'qa', schema_version: 1, state: 'accepted' }, schema_version: 1, status: 'accepted', trace_id: traceId, workspace_id: headers['x-workspace-id'] }),
      status: 202,
    });
  });
  await page.route('**/api/v1/health/summary', async (route) => {
    const headers = route.request().headers();
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ data: { authorization: { code: 'authorization.ready', status: 'ready' }, build_version: 'controlled-build', ingestion: { code: 'ingestion.ready', status: 'ready' }, providers: {}, publication: { code: 'publication.ready', status: 'ready' }, status: 'ready' }, environment_id: headers['x-environment-id'], schema_version: 1, trace_id: traceId, workspace_id: headers['x-workspace-id'] }), status: 200 });
  });

  await page.goto(`/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}&tab=projection&projection_kind=qa`);
  const panel = page.getByRole('region', { name: 'Состояние публикации проекции' });
  await page.getByRole('button', { name: 'Запросить пересборку проекции' }).click();
  await expect(panel.getByText('Запрос принят.', { exact: false })).toBeVisible();
  await expect(panel.getByText('70000000-0000-4000-8000-000000000009')).toBeVisible();
  await expect(panel.getByText('Запрос на восстановление выполняется.')).toHaveCount(2);
  await expect(panel.getByText('Завершено')).toBeVisible({ timeout: 5000 });
  await expect(panel.getByText('Актуальна', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Запросить пересборку проекции' })).toHaveCount(0);

  expect(commandRequests).toHaveLength(1);
  expect(commandRequests[0]?.idempotencyKey).toMatch(/^projection-recovery-/);
  expect(commandRequests[0]?.body).toBe(JSON.stringify({ action: 'rebuild' }));
  expect(commandRequests[0]?.body).not.toMatch(/snapshot|generation|boundary|candidate|namespace|access.?policy/i);
  expect(commandRequests[0]?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(commandRequests[0]?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(statusReads).toBeGreaterThanOrEqual(3);
});

test('test_document_revision_history_direct_url_reloads_exact_selected_revision', async ({ page }) => {
  const requests = await installControlledDocumentDetail(page);
  const historyResponse = page.waitForResponse((response) => response.url().includes('/revisions'));
  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}&tab=revisions&revision=${secondDocument}`,
  );

  await expect(page.getByRole('tab', { name: 'История ревизий', selected: true })).toBeVisible();
  expect((await historyResponse).status()).toBe(200);
  await expect(page.getByText('30000000…0002')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('tab', { name: 'История ревизий', selected: true })).toBeVisible();
  await expect(page.getByText('30000000…0002')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`tab=revisions.*revision=${secondDocument}`));

  expect(requests.length).toBeGreaterThanOrEqual(4);
  expect(requests.every(({ headers }) => headers['x-workspace-id'] === salesWorkspace)).toBe(true);
  expect(requests.every(({ headers }) => headers['x-environment-id'] === stagingEnvironment)).toBe(true);
});

test('test_document_content_inspection_renders_exact_revision_and_locator', async ({ page }) => {
  const requests = await installControlledDocumentDetail(page);
  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}&tab=content&revision=${secondDocument}`,
  );

  await expect(page.getByRole('tab', { name: 'Содержимое', selected: true })).toBeVisible();
  await expect(page.getByText('Synthetic protected content')).toBeVisible();
  await expect(page.getByText(/Страница 2/)).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Содержимое', selected: true })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`tab=content.*revision=${secondDocument}`));

  const inspectionRequest = requests.find(({ url }) => url.includes('/parsed-content'));
  expect(inspectionRequest).toBeDefined();
  expect(inspectionRequest?.url).toContain(`processing_receipt_id=${firstDocument}`);
  expect(inspectionRequest?.headers['x-workspace-id']).toBe(salesWorkspace);
  expect(inspectionRequest?.headers['x-environment-id']).toBe(stagingEnvironment);
  expect(requests.every(({ url }) => !url.includes('Synthetic protected content'))).toBe(true);
});

test('test_revoked_content_authorization_removes_cached_excerpt_for_administrator', async ({ page }) => {
  await installControlledDocumentDetail(page);
  const inspectionRequests: Array<{ headers: Record<string, string>; url: string }> = [];
  let readContentAllowed = true;
  await page.route(
    `**/api/v1/knowledge/documents/${firstDocument}/revisions/${secondDocument}/parsed-content*`,
    async (route) => {
      const request = route.request();
      const headers = request.headers();
      inspectionRequests.push({ headers, url: request.url() });
      if (readContentAllowed) {
        await route.fallback();
        return;
      }
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'spine.authorization.denied',
          retryability: 'never',
          safe_message: 'Authorization is denied.',
          schema_version: 1,
          trace_id: traceId,
          protected_text: 'revoked customer content must not leak',
        }),
        status: 403,
      });
    },
  );

  await page.goto(
    `/knowledge/documents/${firstDocument}?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}&tab=content&revision=${secondDocument}`,
  );
  await expect(page.getByText('Synthetic protected content')).toBeVisible();

  readContentAllowed = false;
  await page.reload();

  await expect(page.getByText('Содержимое недоступно для этой SourceRevision.')).toBeVisible();
  await expect(page.getByText('Synthetic protected content')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('revoked customer content must not leak');
  expect(inspectionRequests.map(({ headers }) => headers['x-workspace-id'])).toEqual([
    salesWorkspace,
    salesWorkspace,
  ]);
  expect(inspectionRequests.every(({ headers }) => headers['x-environment-id'] === stagingEnvironment)).toBe(true);
  expect(inspectionRequests.every(({ url }) => !url.includes('Synthetic protected content'))).toBe(true);
});

test('test_documents_loading_state_is_named_while_the_controlled_response_is_pending', async ({ page }) => {
  let releaseInitialResponse: (() => void) | undefined;
  const initialResponse = new Promise<void>((resolve) => {
    releaseInitialResponse = resolve;
  });
  await page.route('**/api/v1/documents*', async (route) => {
    const headers = route.request().headers();
    await initialResponse;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(controlledDocumentCollection(headers, [])), status: 200 });
  });

  const navigation = page.goto(
    `/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );
  await expect(page.getByRole('region', { name: 'Загрузка' })).toBeVisible();
  releaseInitialResponse?.();
  await navigation;
});

test('test_documents_empty_state_offers_a_safe_retry_action', async ({ page }) => {
  await page.route('**/api/v1/documents*', async (route) => {
    const headers = route.request().headers();
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(controlledDocumentCollection(headers, [])), status: 200 });
  });
  await page.goto(`/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  await expect(page.getByRole('region', { name: 'Пока нет данных' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Пока нет данных' }).getByRole('button', { name: 'Загрузить документы' })).toBeVisible();
});

test('test_documents_validation_failure_hides_protected_diagnostics', async ({ page }) => {
  await page.route('**/api/v1/documents*', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ code: 'spine.validation.invalid', retryability: 'never', safe_message: 'The request is invalid.', schema_version: 1, trace_id: traceId, protected_detail: 'validation provider detail' }),
      status: 422,
    });
  });
  await page.goto(`/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  await expect(page.getByRole('alert')).toContainText('Проверьте данные');
  await expect(page.locator('body')).not.toContainText('validation provider detail');
});

test('test_documents_partial_state_distinguishes_stale_degraded_and_unpublished', async ({ page }) => {
  await page.route('**/api/v1/documents*', async (route) => {
    const headers = route.request().headers();
    const item = controlledDocumentSnapshot({ freshness: 'stale', health: 'degraded', metadataStatus: 'degraded', publicationStatus: 'unpublished' });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(controlledDocumentCollection(headers, [item])), status: 200 });
  });
  await page.goto(`/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  await expect(page.getByRole('alert')).toContainText('Показаны не все данные');
  await expect(page.getByRole('table')).toContainText('Не опубликован');
  await expect(page.getByRole('table')).toContainText('Проекция ограничена');
});

test('test_documents_failed_state_hides_provider_diagnostics', async ({ page }) => {
  await page.route('**/api/v1/documents*', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ code: 'spine.internal.safe_failure', retryability: 'after_delay', safe_message: 'The service is temporarily unavailable.', schema_version: 1, trace_id: traceId, protected_detail: 'provider stack must not leak' }),
      status: 500,
    });
  });
  await page.goto(`/knowledge/documents?fixture=controlled&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  await expect(page.getByRole('alert')).toContainText('Сервис работает с ограничениями');
  await expect(page.locator('body')).not.toContainText('provider stack must not leak');
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

test('test_controlled_query_keeps_the_original_chat_interface', async ({ page }) => {
  await page.goto(
    `/chat/demo-session?fixture=controlled&workspace=${primaryWorkspace}&environment=${localEnvironment}`,
  );

  await expect(page.getByRole('heading', { level: 1, name: 'Чат' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Разделы workspace' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Диагностический чат' })).toHaveCount(0);
  await expect(page.getByText('Не удалось загрузить раздел')).toBeVisible();
});

test('test_chat_admits_question_and_displays_canonical_terminal_outcome', async ({ page }) => {
  let admissionRequest: { body: string; headers: Record<string, string> } | null = null;
  await page.route('**/api/v1/qa/sessions/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          data: {
            schema_version: 1,
            session_id: chatSession,
            workspace_id: primaryWorkspace,
            environment_id: localEnvironment,
            subject_id: '90000000-0000-4000-8000-000000000005',
            version: 1,
            status: 'active',
            created_at: '2026-10-17T10:00:00Z',
            updated_at: '2026-10-17T10:00:00Z',
          },
          schema_version: 1,
          workspace_id: primaryWorkspace,
          environment_id: localEnvironment,
          trace_id: traceId,
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}/messages` && request.method() === 'GET') {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: primaryWorkspace,
          environment_id: localEnvironment,
          items: [],
          next_cursor: null,
          limit: 25,
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}/messages/${chatMessage}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          data: {
            schema_version: 1,
            message_id: chatMessage,
            session_id: chatSession,
            workspace_id: primaryWorkspace,
            environment_id: localEnvironment,
            version: 1,
            ordinal: 1,
            trace_id: traceId,
            run_id: chatRun,
            state: 'terminal',
            terminal_outcome: 'abstained',
            cancelled: false,
            disclosure_eligibility: 'eligible',
            question: 'Где опубликована инструкция?',
            answer: null,
            accepted_presentation: null,
            created_at: '2026-10-17T10:01:00Z',
            updated_at: '2026-10-17T10:01:00Z',
          },
          schema_version: 1,
          workspace_id: primaryWorkspace,
          environment_id: localEnvironment,
          trace_id: traceId,
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, body: '{}' });
  });
  await page.route('**/api/v1/ask/admissions', async (route) => {
    const request = route.request();
    admissionRequest = { body: request.postData() ?? '', headers: request.headers() };
    await route.fulfill({
      contentType: 'application/json',
      status: 202,
      body: JSON.stringify({
        schema_version: 1,
        command_id: chatCommand,
        workspace_id: primaryWorkspace,
        environment_id: localEnvironment,
        trace_id: traceId,
        status: 'accepted',
        replay: false,
        result: {
          schema_version: 1,
          command_id: chatCommand,
          session_id: chatSession,
          message_id: chatMessage,
          run_id: chatRun,
          status: 'admitted',
        },
      }),
    });
  });

  await page.goto(
    `/chat/${chatSession}?fixture=chat-api&workspace=${primaryWorkspace}&environment=${localEnvironment}`,
  );
  await page.getByRole('textbox', { name: 'Вопрос' }).fill('Где опубликована инструкция?');
  await page.getByRole('button', { name: 'Отправить' }).click();

  await expect(page.getByText('Недостаточно данных для ответа')).toBeVisible();
  await expect(page.getByText('Где опубликована инструкция?')).toBeVisible();
  expect(admissionRequest).not.toBeNull();
  expect(JSON.parse(admissionRequest!.body)).toEqual({
    schema_version: 1,
    session_id: chatSession,
    question: 'Где опубликована инструкция?',
  });
  expect(admissionRequest!.headers['idempotency-key']).toBeTruthy();
  expect(admissionRequest!.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(admissionRequest!.headers['x-environment-id']).toBe(localEnvironment);
});

test('test_chat_sends_question_displays_answer_inspects_citation_and_records_feedback', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const answer = 'Политика опубликована в базе знаний.';
  const answerDigest = createHash('sha256').update(answer, 'utf8').digest('hex');
  const citationRequests: Array<{ headers: Record<string, string>; url: string }> = [];
  const feedbackRequests: Array<{ headers: Record<string, string>; body: string }> = [];
  const targetScopeId = '90000000-0000-4000-8000-000000000091';
  const observedCoverageId = '90000000-0000-4000-8000-000000000092';
  let askAdmitted = false;
  let admissionRequest: { headers: Record<string, string>; body: string } | null = null;
  const message = {
    schema_version: 1,
    message_id: chatMessage,
    session_id: chatSession,
    workspace_id: primaryWorkspace,
    environment_id: localEnvironment,
    version: 1,
    ordinal: 1,
    trace_id: traceId,
    run_id: chatRun,
    state: 'terminal',
    terminal_outcome: 'answered',
    cancelled: false,
    disclosure_eligibility: 'eligible',
    question: 'Где опубликована инструкция?',
    answer,
    accepted_presentation: {
      schema_version: 'accepted-answer-presentation:v1',
      answer_id: chatAnswer,
      answer_text_sha256: answerDigest,
      attempt_id: chatAttempt,
      message_id: chatMessage,
      run_id: chatRun,
      claims: [{
        schema_version: 'accepted-answer-claim-presentation:v1',
        claim_id: chatClaim,
        kind: 'factual',
        start_offset: 0,
        end_offset: answer.length,
      }],
      relationships: [{
        schema_version: 'accepted-answer-relationship-presentation:v1',
        relationship_id: chatRelationship,
        claim_id: chatClaim,
        kind: 'independent',
        citation_ids: [chatCitation],
        related_claim_id: null,
      }],
      citation_ids: [chatCitation],
    },
    completeness: {
      schema_version: 'answer-completeness-presentation:v1',
      target_scope_id: targetScopeId,
      target_scope_version: 'target-completeness-scope:v1',
      observed_coverage_id: observedCoverageId,
      observed_coverage_version: 'observed-coverage:v1',
      coverage_status: 'insufficient',
      partiality_status: 'partial',
      conflict_status: 'presented',
      partial_fallback_authority: 'current_request',
      serving_decision: 'serve',
      validation_decision: 'grounded_conflict',
    },
    created_at: '2026-10-17T10:01:00Z',
    updated_at: '2026-10-17T10:01:00Z',
  };

  await page.route('**/api/v1/qa/sessions/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          data: {
            schema_version: 1,
            session_id: chatSession,
            workspace_id: primaryWorkspace,
            environment_id: localEnvironment,
            subject_id: '90000000-0000-4000-8000-000000000010',
            version: 1,
            status: 'active',
            created_at: '2026-10-17T10:00:00Z',
            updated_at: '2026-10-17T10:00:00Z',
          },
          schema_version: 1,
          workspace_id: primaryWorkspace,
          environment_id: localEnvironment,
          trace_id: traceId,
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}/messages/${chatMessage}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          data: message,
          schema_version: 1,
          workspace_id: primaryWorkspace,
          environment_id: localEnvironment,
          trace_id: traceId,
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}/messages`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: primaryWorkspace,
          environment_id: localEnvironment,
          items: askAdmitted ? [message] : [],
          next_cursor: null,
          limit: 25,
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, body: '{}' });
  });
  await page.route('**/api/v1/ask/admissions', async (route) => {
    const request = route.request();
    admissionRequest = { headers: request.headers(), body: request.postData() ?? '' };
    askAdmitted = true;
    await route.fulfill({
      contentType: 'application/json',
      status: 202,
      body: JSON.stringify({
        schema_version: 1,
        command_id: chatCommand,
        workspace_id: primaryWorkspace,
        environment_id: localEnvironment,
        trace_id: traceId,
        status: 'accepted',
        replay: false,
        result: {
          schema_version: 1,
          command_id: chatCommand,
          session_id: chatSession,
          message_id: chatMessage,
          run_id: chatRun,
          status: 'admitted',
        },
      }),
    });
  });
  await page.route('**/api/v1/qa/citations/**', async (route) => {
    const request = route.request();
    citationRequests.push({ headers: request.headers(), url: request.url() });
    await route.fulfill({
      contentType: 'application/json',
      status: 200,
      body: JSON.stringify({
        schema_version: 1,
        workspace_id: primaryWorkspace,
        environment_id: localEnvironment,
        trace_id: traceId,
        data: {
          schema_version: 'citation-inspection-result:v1',
          status: 'available',
          reason: 'available',
          evidence: {
            schema_version: 'citation-inspection-evidence:v1',
            answer_id: chatAnswer,
            citation_id: chatCitation,
            context_bundle: {
              schema_version: 'context-bundle-lineage-ref:v1',
              attempt_id: chatAttempt,
              run_id: chatRun,
              workspace_id: primaryWorkspace,
              environment_id: localEnvironment,
              bundle: {
                bundle_id: '90000000-0000-4000-8000-000000000011',
                bundle_version: 1,
                bundle_digest: 'd'.repeat(64),
              },
            },
            evidence_reference: {
              schema_version: 'context-evidence-reference:v2',
              workspace_id: primaryWorkspace,
              environment_id: localEnvironment,
              evidence_id: 'synthetic-evidence',
              projection_snapshot_id: '90000000-0000-4000-8000-000000000012',
              activation_generation: 1,
              source_object_id: '90000000-0000-4000-8000-000000000013',
              source_revision_id: '90000000-0000-4000-8000-000000000014',
              original_sha256: 'a'.repeat(64),
              revision_digest: 'b'.repeat(64),
              processing_receipt_id: '90000000-0000-4000-8000-000000000015',
              projected_kind: 'chunk',
              locator_contract_version: 'pdf-page-extracted-codepoint:v1',
              locator: { kind: 'page', page_number: 2 },
              locator_digest: 'c'.repeat(64),
              evidence_digest: 'c'.repeat(64),
              status: 'valid',
            },
            excerpt: 'Синтетический фрагмент из разрешённой ревизии.',
            historical: false,
            locator_digest: 'c'.repeat(64),
            locator_reference: 'page:2#0-18',
            locator_schema: 'pdf-page-extracted-codepoint:v1',
            original_sha256: 'a'.repeat(64),
            revision_digest: 'b'.repeat(64),
            revision_number: 2,
            source_object_id: '90000000-0000-4000-8000-000000000013',
            source_revision_id: '90000000-0000-4000-8000-000000000014',
            workspace_id: primaryWorkspace,
            environment_id: localEnvironment,
          },
        },
      }),
    });
  });
  await page.route('**/api/v1/qa/feedback', async (route) => {
    const request = route.request();
    feedbackRequests.push({ headers: request.headers(), body: request.postData() ?? '' });
    await route.fulfill({
      contentType: 'application/json',
      status: 200,
      body: JSON.stringify({
        schema_version: 1,
        workspace_id: primaryWorkspace,
        environment_id: localEnvironment,
        trace_id: traceId,
        result: {
          schema_version: 1,
          status: 'recorded',
          feedback_id: '90000000-0000-4000-8000-000000000016',
          feedback_version: 1,
          target_kind: 'answer',
          target_id: chatAnswer,
          run_id: chatRun,
          message_id: chatMessage,
          citation_id: null,
          outcome: 'answer.helpful',
          audit_event_id: null,
        },
      }),
    });
  });

  await page.goto(
    `/chat/${chatSession}?fixture=chat-api&workspace=${primaryWorkspace}&environment=${localEnvironment}`,
  );

  await expect(page.getByRole('heading', { name: `Сессия · ${chatSession.slice(-8)}` })).toBeVisible();
  const question = page.getByRole('textbox', { name: 'Вопрос' });
  await expect(question).toBeVisible();
  await question.fill('Где опубликована инструкция?');
  await page.getByRole('button', { name: 'Отправить' }).focus();
  await page.keyboard.press('Enter');

  await expect(page.locator('[data-accepted-answer-text]')).toContainText(answer);
  const completeness = page.getByRole('button', { name: /несовместимые подтверждённые позиции/ });
  await expect(completeness).toBeVisible();
  await completeness.click();
  await expect(page.getByText(/разрешено текущим запросом/)).toBeVisible();
  await expect(page.locator('body')).not.toContainText(targetScopeId);
  await expect(page.locator('body')).not.toContainText(observedCoverageId);
  await expect(page.locator('body')).not.toContainText(/\d+ (источник|источника|источников)/i);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(admissionRequest).not.toBeNull();
  expect(JSON.parse(admissionRequest!.body)).toEqual({
    schema_version: 1,
    session_id: chatSession,
    question: 'Где опубликована инструкция?',
  });
  expect(admissionRequest!.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(admissionRequest!.headers['x-environment-id']).toBe(localEnvironment);
  expect(admissionRequest!.headers['idempotency-key']).toBeTruthy();
  await expect(page.locator('[data-claim-id]')).toHaveAttribute('data-end-utf16', String(answer.length));
  await page.getByRole('button', { name: 'Открыть источник 1 · Независимое подтверждение' }).click();
  await expect(page.getByText('Синтетический фрагмент из разрешённой ревизии.')).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть панель' }).click();
  await page.getByRole('group', { name: 'Оценка ответа' }).getByRole('button', { name: 'Полезно' }).click();
  await page.getByRole('button', { name: 'Отправить отзыв' }).click();
  await expect(page.getByText('Спасибо, отзыв сохранён.')).toBeVisible();

  expect(citationRequests).toHaveLength(1);
  expect(new URL(citationRequests[0]!.url).search).toBe('');
  expect(citationRequests[0]!.headers.authorization).toBe('Bearer controlled-chat-api-token');
  expect(citationRequests[0]!.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(citationRequests[0]!.headers['x-environment-id']).toBe(localEnvironment);
  expect(feedbackRequests).toHaveLength(1);
  expect(feedbackRequests[0]!.headers['x-workspace-id']).toBe(primaryWorkspace);
  expect(feedbackRequests[0]!.headers['x-environment-id']).toBe(localEnvironment);
  expect(feedbackRequests[0]!.headers['idempotency-key']).toBeTruthy();
  expect(JSON.parse(feedbackRequests[0]!.body)).toEqual({
    target_kind: 'answer',
    target_id: chatAnswer,
    outcome: 'answer.helpful',
    run_id: chatRun,
    message_id: chatMessage,
  });
  await expect(page.locator('body')).not.toContainText(/provider stack|candidate answer|private diagnostic/i);
});

test('test_controlled_query_without_api_fails_closed_in_the_original_chat', async ({ page }) => {
  await page.goto(
    `/chat/demo-session?fixture=controlled&workspace=${primaryWorkspace}&environment=${localEnvironment}`,
  );

  await expect(page.getByRole('heading', { level: 1, name: 'Чат' })).toBeVisible();
  await expect(page.getByText('Не удалось загрузить раздел')).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Вопрос' })).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText(/provider|stack|secret/i);
});

async function choose(page: Page, label: string, option: string) {
  const select = page.getByRole('combobox', { name: label });
  if (await select.count()) {
    await select.click();
    await page.getByRole('option', { name: option, exact: true }).click();
    return;
  }
  await page.getByRole('button', { name: /Рабочая область:/ }).click();
  await page.getByRole('menuitemradio', { name: option, exact: true }).click();
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
    const isSalesScope = isOwnedScope(headers, salesWorkspace, stagingEnvironment);
    const isSecondPage = isSalesScope && url.searchParams.get('cursor') === 'documents-next';
    const sourceObjectId = isSalesScope
      ? (isSecondPage ? secondDocument : firstDocument)
      : primaryDocument;
    const body = controlledDocumentCollection(
      headers,
      [controlledDocumentSnapshot({ sourceObjectId })],
      isSalesScope && !isSecondPage ? 'documents-next' : null,
    );
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(body),
      status: 200,
    });
  });
  return requests;
}

function controlledDocumentSnapshot({
  freshness = 'current',
  health = 'healthy',
  metadataStatus = 'available',
  publicationStatus = 'published',
  sourceObjectId = firstDocument,
}: {
  freshness?: components['schemas']['ProjectionFreshness'];
  health?: components['schemas']['ProjectionHealth'];
  metadataStatus?: components['schemas']['DocumentOperationalMetadata']['status'];
  publicationStatus?: components['schemas']['ProjectionPublicationStatus'];
  sourceObjectId?: string;
} = {}): components['schemas']['DocumentSnapshot'] {
  return {
    created_at: '2026-01-02T03:04:05Z',
    operational_metadata: {
      access_policy: { generation: 3, policy_version_id: '51000000-0000-0000-0000-000000000025', schema_version: 1, version: 2 },
      freshness,
      health,
      lifecycle: 'current',
      processing_state: 'processed',
      publication_status: publicationStatus,
      schema_version: 1,
      security_domain: { binding_version: 1, reference: 'security-domain:controlled', schema_version: 1, version: 'r1' },
      source_object_id: sourceObjectId,
      status: metadataStatus,
    },
    publication: { freshness, health, projection_kind: 'qa', status: publicationStatus },
    schema_version: 1,
    source_kind: 'document',
    source_object_id: sourceObjectId,
  };
}

function controlledDocumentCollection(
  headers: Record<string, string>,
  items: components['schemas']['DocumentSnapshot'][],
  nextCursor: string | null = null,
): components['schemas']['ApiCollectionEnvelope_DocumentSnapshot_'] {
  return {
    environment_id: headers['x-environment-id'] ?? '',
    items,
    limit: 25,
    next_cursor: nextCursor,
    schema_version: 1,
    trace_id: traceId,
    workspace_id: headers['x-workspace-id'] ?? '',
  };
}

function controlledRevision({
  kind = 'content',
  lifecycle = 'current',
  observedAt = '2026-01-02T03:00:00Z',
  predecessorRevisionId = null,
  sourceRevisionId = secondDocument,
  supersededByRevisionId = null,
}: {
  kind?: components['schemas']['RevisionKind'];
  lifecycle?: components['schemas']['RevisionLifecycle'];
  observedAt?: string;
  predecessorRevisionId?: string | null;
  sourceRevisionId?: string;
  supersededByRevisionId?: string | null;
} = {}): components['schemas']['RevisionSnapshot'] {
  return {
    accepted_at: '2026-01-02T04:00:00Z',
    accepted_generation: 2,
    admission_status: 'admitted',
    content: null,
    deletion_reason: kind === 'tombstone' ? 'source_deleted' : null,
    kind,
    lifecycle,
    observed_at: observedAt,
    predecessor_revision_id: predecessorRevisionId,
    processing_references: [],
    projection_references: [],
    schema_version: 1,
    source_revision_id: sourceRevisionId,
    superseded_by_revision_id: supersededByRevisionId,
  };
}

async function installControlledDocumentDetail(page: Page, withProjectionCorrelation = false) {
  const requests: Array<{ url: string; headers: Record<string, string> }> = [];
  const fixtures = [
    { environmentId: stagingEnvironment, sourceObjectId: firstDocument, sourceRevisionId: secondDocument, workspaceId: salesWorkspace },
    { environmentId: localEnvironment, sourceObjectId: primaryDocument, sourceRevisionId: primaryRevision, workspaceId: primaryWorkspace },
  ] as const;
  for (const fixture of fixtures) {
  await page.route(`**/api/v1/documents/${fixture.sourceObjectId}`, async (route) => {
    const request = route.request();
    requests.push({ url: request.url(), headers: request.headers() });
    const headers = request.headers();
    if (!isOwnedScope(headers, fixture.workspaceId, fixture.environmentId)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
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
            ingestion_item_id: fixture.sourceRevisionId,
            ingestion_run_id: fixture.sourceObjectId,
            processing_receipt_id: fixture.sourceObjectId,
            state: 'processed' as const,
          },
        ],
        projection_references: withProjectionCorrelation
          ? [{
              projection_candidate_id: 'a1000000-0000-4000-8000-000000000001',
              projection_kind: 'qa',
              projection_run_id: projectionRun,
              state: 'failed' as const,
            }]
          : [],
        schema_version: 1 as const,
        source_revision_id: fixture.sourceRevisionId,
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
        source_object_id: fixture.sourceObjectId,
        source_revision_id: fixture.sourceRevisionId,
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
      source_object_id: fixture.sourceObjectId,
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
  await page.route(`**/api/v1/documents/${fixture.sourceObjectId}/revisions*`, async (route) => {
    const request = route.request();
    requests.push({ url: request.url(), headers: request.headers() });
    const headers = request.headers();
    if (!isOwnedScope(headers, fixture.workspaceId, fixture.environmentId)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
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
      processing_references: [{
        coverage: 'complete' as const,
        ingestion_item_id: fixture.sourceRevisionId,
        ingestion_run_id: fixture.sourceObjectId,
        processing_receipt_id: fixture.sourceObjectId,
        state: 'processed' as const,
      }],
      projection_references: [],
      schema_version: 1 as const,
      source_revision_id: fixture.sourceRevisionId,
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
  await page.route(`**/api/v1/knowledge/documents/${fixture.sourceObjectId}/revisions/${fixture.sourceRevisionId}/parsed-content*`, async (route) => {
    const request = route.request();
    requests.push({ url: request.url(), headers: request.headers() });
    const headers = request.headers();
    if (!isOwnedScope(headers, fixture.workspaceId, fixture.environmentId)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
    const response = {
      data: {
        artifact_digest: 'controlled-artifact-digest',
        artifact_id: fixture.sourceObjectId,
        items: [{
          heading_path: ['Controlled section'],
          locator: {
            end_codepoint: 26,
            kind: 'page_text' as const,
            page_number: 2,
            start_codepoint: 0,
          },
          segment_digest: 'controlled-segment-digest',
          segment_id: fixture.sourceObjectId,
          source_object_id: fixture.sourceObjectId,
          source_revision_digest: 'controlled-revision-digest',
          source_revision_id: fixture.sourceRevisionId,
          text: 'Synthetic protected content',
          text_digest: 'controlled-text-digest',
        }],
        locator_contract_version: 'controlled-locator-v1',
        next_cursor: null,
        original_sha256: 'controlled-original-sha256',
        parser_config_reference: 'controlled-parser-config',
        parser_name: 'controlled-parser',
        parser_version: 'controlled-parser-v1',
        processing_receipt_id: fixture.sourceObjectId,
        schema_version: 'knowledge-parsed-content-inspection:v1' as const,
        source_object_id: fixture.sourceObjectId,
        source_revision_digest: 'controlled-revision-digest',
        source_revision_id: fixture.sourceRevisionId,
        status: 'ready' as const,
      },
      environment_id: headers['x-environment-id'] ?? '',
      schema_version: 1 as const,
      trace_id: traceId,
      workspace_id: headers['x-workspace-id'] ?? '',
    } satisfies components['schemas']['ApiResponseEnvelope_ParsedContentInspectionResponse_'];
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(response), status: 200 });
  });
  }
  return requests;
}

function controlledLifecycleReceipt(
  operationKind: 'replacement' | 'tombstone',
  status: 'accepted_processing_pending' | 'processing_dispatched' | 'completed' = 'completed',
  sourceObjectId = firstDocument,
  acceptedRevisionId = secondDocument,
) {
  return {
    accepted_at: '2026-01-02T04:05:00Z',
    accepted_revision_id: acceptedRevisionId,
    canonical_acceptance_id: sourceObjectId,
    completed_at: status === 'completed' ? '2026-01-02T04:06:00Z' : null,
    expected_current_revision_id: acceptedRevisionId,
    idempotency_receipt_id: sourceObjectId,
    operation_id: sourceObjectId,
    operation_kind: operationKind,
    processing_run_id: sourceObjectId,
    replay: false,
    retryable: false,
    safe_failure_code: null,
    schema_version: 1,
    source_object_id: sourceObjectId,
    status,
    status_url: `/api/v1/documents/lifecycle/operations/${sourceObjectId}`,
  } satisfies components['schemas']['SourceLifecycleOperationResponse'];
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

function processingCommandReceipt(input: {
  failureIds: string[];
  itemId: string;
  operationId: string;
  runId: string;
  sourceObjectId: string;
  sourceRevisionId: string;
  status: components['schemas']['ProcessingCommandStatus'];
  triggerKind?: components['schemas']['ProcessingCommandResponse']['trigger_kind'];
  replay?: boolean;
}): components['schemas']['ProcessingCommandResponse'] {
  return {
    failure_ids: input.failureIds,
    item_id: input.itemId,
    next_action: input.status === 'ready' ? 'none' : 'poll',
    operation_id: input.operationId,
    processing_receipt_id: input.status === 'ready' ? '80000000-0000-4000-8000-000000000006' : null,
    replay: input.replay ?? false,
    run_id: input.runId,
    schema_version: 1,
    source_object_id: input.sourceObjectId,
    source_revision_id: input.sourceRevisionId,
    status: input.status,
    status_url: `/api/v1/documents/processing/operations/${input.operationId}`,
    trigger_kind: input.triggerKind ?? 'retry',
  };
}

test('test_chat_run_citation_navigation_preserves_scope_back_reload_and_denies_other_workspace', async ({ page }) => {
  const answer = 'Ответ из sales workspace.';
  const answerDigest = createHash('sha256').update(answer, 'utf8').digest('hex');
  const runRequests: Array<Record<string, string>> = [];
  const citationRequests: Array<Record<string, string>> = [];
  const correlationRequests: Array<Record<string, string>> = [];
  await page.route('**/api/v1/diagnostics/operational-correlation*', async (route) => {
    const request = route.request();
    const headers = request.headers();
    const url = new URL(request.url());
    correlationRequests.push(headers);
    if (!isOwnedScope(headers, salesWorkspace, stagingEnvironment)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
    expect(url.searchParams.get('reference_kind')).toBe('run');
    expect(url.searchParams.get('reference_id')).toBe(chatRun);
    await route.fulfill({
      contentType: 'application/json',
      status: 200,
      body: JSON.stringify({
        authorization_audit_event_id: '70000000-0000-4000-8000-000000000001',
        data: {
          conflict_codes: [],
          correlation_ids: [],
          coverage: 'complete',
          environment_id: stagingEnvironment,
          lineage: [],
          missing_sources: [],
          records: [{
            deduplication_key: `run:${chatRun}`,
            environment_id: stagingEnvironment,
            message_id: chatMessage,
            observed_at: '2026-10-17T10:01:00Z',
            run_id: chatRun,
            run_outcome: 'answered',
            run_state: 'completed',
            schema_version: 'operational-correlation-record:v1',
            source_identity: 'canonical.answer-question-run:v1',
            source_kind: 'canonical_run',
            source_record_id: chatRun,
            source_schema_version: 'answer-question-run:v1',
            trace_id: traceId,
            workspace_id: salesWorkspace,
          }],
          reference_id: chatRun,
          reference_kind: 'run',
          schema_version: 'operational-correlation-view:v1',
          trace_ids: [traceId],
          workspace_id: salesWorkspace,
        },
        environment_id: stagingEnvironment,
        schema_version: 1,
        trace_id: traceId,
        workspace_id: salesWorkspace,
      }),
    });
  });
  const message = {
    schema_version: 1,
    message_id: chatMessage,
    session_id: chatSession,
    workspace_id: salesWorkspace,
    environment_id: stagingEnvironment,
    version: 1,
    ordinal: 1,
    trace_id: traceId,
    run_id: chatRun,
    state: 'terminal',
    terminal_outcome: 'answered',
    cancelled: false,
    disclosure_eligibility: 'eligible',
    question: 'Где опубликована инструкция?',
    answer,
    accepted_presentation: {
      schema_version: 'accepted-answer-presentation:v1',
      answer_id: chatAnswer,
      answer_text_sha256: answerDigest,
      attempt_id: chatAttempt,
      message_id: chatMessage,
      run_id: chatRun,
      claims: [{
        schema_version: 'accepted-answer-claim-presentation:v1',
        claim_id: chatClaim,
        kind: 'factual',
        start_offset: 0,
        end_offset: answer.length,
      }],
      relationships: [{
        schema_version: 'accepted-answer-relationship-presentation:v1',
        relationship_id: chatRelationship,
        claim_id: chatClaim,
        kind: 'independent',
        citation_ids: [chatCitation],
        related_claim_id: null,
      }],
      citation_ids: [chatCitation],
    },
    created_at: '2026-10-17T10:01:00Z',
    updated_at: '2026-10-17T10:01:00Z',
  };
  const evidence = {
    activation_generation: 4,
    citation_id: chatCitation,
    environment_id: stagingEnvironment,
    evidence_digest: 'sha256:evidence',
    evidence_id: 'evidence-sales-1',
    locator_kind: 'page',
    locator_schema: 'page.v1',
    locator_value: '4',
    projection_snapshot_id: '90000000-0000-4000-8000-000000000011',
    revision_number: 2,
    source_object_id: '90000000-0000-4000-8000-000000000012',
    source_revision_id: '90000000-0000-4000-8000-000000000013',
    workspace_id: salesWorkspace,
  };

  await page.route('**/api/v1/qa/runs**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = request.headers();
    runRequests.push(headers);
    if (!isOwnedScope(headers, salesWorkspace, stagingEnvironment)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
    if (url.pathname === '/api/v1/qa/runs') {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          limit: 25,
          next_cursor: null,
          items: [{
            attempt_count: 1,
            completed_at: '2026-10-17T10:01:00Z',
            created_at: '2026-10-17T10:00:00Z',
            environment_id: stagingEnvironment,
            final_attempt_id: chatAttempt,
            message_id: chatMessage,
            outcome: 'answered',
            run_id: chatRun,
            schema_version: 1,
            session_id: chatSession,
            state: 'completed',
            subject_id: '90000000-0000-4000-8000-000000000010',
            subject_kind: 'human',
          }],
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/runs/${chatRun}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          data: {
            accepted_evidence: [evidence],
            attempts: [],
            causation_id: null,
            command_digest: 'sha256:command',
            completed_at: '2026-10-17T10:01:00Z',
            correlation_id: null,
            created_at: '2026-10-17T10:00:00Z',
            diagnostic_events: [],
            environment_id: stagingEnvironment,
            evidence_eligible: true,
            final_attempt_id: chatAttempt,
            input_artifact_ids: [],
            message_id: chatMessage,
            outcome: 'answered',
            output_artifact_ids: [],
            run_id: chatRun,
            schema_version: 1,
            session_id: chatSession,
            state: 'completed',
            subject_id: '90000000-0000-4000-8000-000000000010',
            subject_kind: 'human',
            trace_id: traceId,
            warnings: [],
            workspace_id: salesWorkspace,
          },
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, body: '{}' });
  });

  await page.route('**/api/v1/qa/sessions/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = request.headers();
    if (!isOwnedScope(headers, salesWorkspace, stagingEnvironment)) {
      await fulfillForeignScopeDenied(route);
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          data: {
            schema_version: 1,
            session_id: chatSession,
            workspace_id: salesWorkspace,
            environment_id: stagingEnvironment,
            subject_id: '90000000-0000-4000-8000-000000000010',
            version: 1,
            status: 'active',
            created_at: '2026-10-17T10:00:00Z',
            updated_at: '2026-10-17T10:01:00Z',
          },
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}/messages/${chatMessage}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          data: message,
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}/messages`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          limit: 25,
          next_cursor: null,
          items: [message],
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, body: '{}' });
  });
  await page.route('**/api/v1/qa/citations/**', async (route) => {
    citationRequests.push(route.request().headers());
    await route.fulfill({
      contentType: 'application/json',
      status: 200,
      body: JSON.stringify({
        schema_version: 1,
        workspace_id: salesWorkspace,
        environment_id: stagingEnvironment,
        trace_id: traceId,
        data: {
          schema_version: 'citation-inspection-result:v1',
          status: 'unavailable',
          reason: 'citation_unavailable',
        },
      }),
    });
  });

  await page.route(
    (url) => url.pathname === '/api/v1/qa/sessions',
    async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          limit: 25,
          next_cursor: null,
          items: [{
            schema_version: 1,
            session_id: chatSession,
            workspace_id: salesWorkspace,
            environment_id: stagingEnvironment,
            subject_id: '90000000-0000-4000-8000-000000000010',
            version: 1,
            status: 'active',
            created_at: '2026-10-17T10:00:00Z',
            updated_at: '2026-10-17T10:01:00Z',
          }],
        }),
      });
    },
  );

  await page.goto(`/chat/${chatSession}?fixture=chat-api&workspace=${salesWorkspace}&environment=${stagingEnvironment}`);
  await page.getByRole('button', { name: 'Открыть запуск Q&A' }).click();
  await expect(page).toHaveURL(new RegExp(`/qa/runs/${chatRun}.*workspace=${salesWorkspace}.*environment=${stagingEnvironment}`));
  await expect(page.getByRole('heading', { level: 2, name: 'Детали запуска Q&A' })).toBeVisible();
  await page.getByRole('tab', { name: 'Диагностика' }).click();
  await expect(page.getByText('Источники корреляции доступны')).toBeVisible();
  await expect(page.getByText('Канонический запуск Q&A')).toBeVisible();
  expect(correlationRequests[0]?.['x-workspace-id']).toBe(salesWorkspace);
  expect(correlationRequests[0]?.['x-environment-id']).toBe(stagingEnvironment);
  await expect(page.locator('body')).not.toContainText('provider stack trace');
  await page.getByRole('tab', { name: /Evidence/ }).click();
  await page.getByRole('button', { name: 'Открыть источник в чате' }).click();
  await expect(page).toHaveURL(new RegExp(`/chat/${chatSession}.*message_id=${chatMessage}.*run_id=${chatRun}.*citation_id=${chatCitation}.*return_run_id=${chatRun}`));
  await expect(page.getByRole('dialog', { name: 'Инспектор источника' })).toBeVisible();
  await expect(page.getByText('Содержимое скрыто по текущим правилам доступа.').first()).toBeVisible();
  await expect(page.locator('body')).not.toContainText(answer);
  await page.goto(`${page.url()}&fixture=chat-api`);
  await page.reload();
  await expect(page.getByText('Содержимое скрыто по текущим правилам доступа.').first()).toBeVisible();
  await expect(page.getByRole('button', { name: /Рабочая область: Продажи, Среда: Staging/ })).toBeVisible();
  await page.getByRole('button', { name: 'Вернуться к запуску Q&A' }).click();
  await expect(page).toHaveURL(new RegExp(`/qa/runs/${chatRun}.*workspace=${salesWorkspace}.*environment=${stagingEnvironment}`));

  await choose(page, 'Рабочая область', 'Основная');
  await expect(page).toHaveURL(new RegExp(`/qa/runs/${chatRun}.*workspace=${primaryWorkspace}.*environment=${stagingEnvironment}`));
  await expect(page.getByRole('region', { name: 'Детали запуска Q&A' })).toContainText('Не удалось загрузить раздел');
  await expect(page.locator('body')).not.toContainText(answer);
  expect(runRequests.some((headers) => headers['x-workspace-id'] === primaryWorkspace)).toBe(true);
  expect(citationRequests.every((headers) => headers['x-workspace-id'] === salesWorkspace)).toBe(true);
});

test('test_chat_session_navigation_preserves_page_and_uses_sequential_mobile_panels', async ({ page }) => {
  const secondSession = '90000000-0000-4000-8000-000000000010';
  await installChatWorkspaceRoutes(page, secondSession);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(
    `/chat?fixture=chat-api&workspace=${salesWorkspace}&environment=${stagingEnvironment}&session_cursor=page-2`,
  );

  await page.getByRole('button', { name: 'Показать список сессий' }).click();
  const firstSessionLink = page.getByRole('list', { name: 'Сессии вопросов и ответов' }).getByRole('listitem').filter({ hasText: chatSession.slice(-8) });
  await expect(firstSessionLink).toBeVisible();
  await firstSessionLink.click();
  await expect(page).toHaveURL(new RegExp(`/chat/${chatSession}.*session_cursor=page-2`));
  await expect(page.getByRole('heading', { name: new RegExp(chatSession.slice(-8)) })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await page.getByRole('button', { name: 'Показать список сессий' }).click();
  await expect(page).toHaveURL(new RegExp(`/chat/${chatSession}.*session_cursor=page-2`));
  const secondSessionLink = page.getByRole('list', { name: 'Сессии вопросов и ответов' }).getByRole('listitem').filter({ hasText: secondSession.slice(-8) });
  await expect(secondSessionLink).toBeVisible();
  await secondSessionLink.click();
  await expect(page).toHaveURL(new RegExp(`/chat/${secondSession}.*session_cursor=page-2`));
  await expect(page.getByRole('heading', { name: new RegExp(secondSession.slice(-8)) })).toBeVisible();
});

test('test_chat_run_summary_inspector_restores_focus_after_escape', async ({ page }) => {
  await installChatWorkspaceRoutes(page);
  await page.goto(
    `/chat/${chatSession}?fixture=chat-api&workspace=${salesWorkspace}&environment=${stagingEnvironment}`,
  );

  const runSummaryTrigger = page.getByRole('button', { name: 'Показать сводку запуска' });
  await runSummaryTrigger.click();
  await expect(page).toHaveURL(new RegExp(`message_id=${chatMessage}.*run_id=${chatRun}.*inspector=run`));
  await expect(page.getByRole('dialog', { name: 'Безопасная сводка запуска' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Закрыть панель' })).toBeFocused();

  await page.keyboard.press('Escape');

  await expect(page.getByRole('dialog', { name: 'Безопасная сводка запуска' })).toHaveCount(0);
  await expect(runSummaryTrigger).toBeFocused();
});

async function installChatWorkspaceRoutes(
  page: Page,
  secondSession = '90000000-0000-4000-8000-000000000010',
): Promise<void> {
  const sessionRecord = (
    sessionId: string,
  ): components['schemas']['QASessionResponse'] => ({
    schema_version: 1,
    session_id: sessionId,
    workspace_id: salesWorkspace,
    environment_id: stagingEnvironment,
    subject_id: '90000000-0000-4000-8000-000000000011',
    version: 1,
    status: 'active',
    created_at: '2026-10-17T10:00:00Z',
    updated_at: '2026-10-17T10:01:00Z',
  });
  const message = {
    schema_version: 1,
    message_id: chatMessage,
    session_id: chatSession,
    workspace_id: salesWorkspace,
    environment_id: stagingEnvironment,
    version: 1,
    ordinal: 1,
    trace_id: traceId,
    run_id: chatRun,
    state: 'terminal',
    terminal_outcome: 'abstained',
    cancelled: false,
    disclosure_eligibility: 'eligible',
    question: 'Достаточно ли оснований для ответа?',
    answer: null,
    accepted_presentation: null,
    created_at: '2026-10-17T10:01:00Z',
    updated_at: '2026-10-17T10:01:00Z',
  } satisfies components['schemas']['QAMessageResponse'];

  await page.route('**/api/v1/qa/sessions**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/qa/sessions') {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          limit: 25,
          next_cursor: null,
          items: [sessionRecord(chatSession), sessionRecord(secondSession)],
        }),
      });
      return;
    }
    if (url.pathname === `/api/v1/qa/sessions/${chatSession}/messages/${chatMessage}`) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          data: message,
        }),
      });
      return;
    }
    const messagesMatch = url.pathname.match(/^\/api\/v1\/qa\/sessions\/([^/]+)\/messages$/);
    if (messagesMatch) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          limit: 25,
          next_cursor: null,
          items: messagesMatch[1] === chatSession ? [message] : [],
        }),
      });
      return;
    }
    const sessionMatch = url.pathname.match(/^\/api\/v1\/qa\/sessions\/([^/]+)$/);
    if (sessionMatch) {
      await route.fulfill({
        contentType: 'application/json',
        status: 200,
        body: JSON.stringify({
          schema_version: 1,
          workspace_id: salesWorkspace,
          environment_id: stagingEnvironment,
          trace_id: traceId,
          data: sessionRecord(sessionMatch[1] ?? chatSession),
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, body: '{}' });
  });
}

function isOwnedScope(
  headers: Record<string, string>,
  workspaceId: string,
  environmentId: string,
): boolean {
  return headers['x-workspace-id'] === workspaceId
    && headers['x-environment-id'] === environmentId;
}

async function fulfillForeignScopeDenied(route: Route): Promise<void> {
  await route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      code: 'spine.authorization.denied',
      retryability: 'never',
      safe_message: 'Authorization is denied.',
      schema_version: 1,
      trace_id: traceId,
    }),
    status: 403,
  });
}
