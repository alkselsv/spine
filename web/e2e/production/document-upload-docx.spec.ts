import { test, type Locator, type Page } from '@playwright/test';
import { resolve } from 'node:path';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const DOCX_DOCUMENT_PATH = process.env.SPINE_E2E_DOCX_DOCUMENT_PATH;

test.skip(!DOCX_DOCUMENT_PATH, 'SPINE_E2E_DOCX_DOCUMENT_PATH is not configured');

test('test_real_browser_uploads_docx_through_parsing_and_projection', async ({ page }) => {
  test.setTimeout(20 * 60_000);
  const documentPath = resolve(DOCX_DOCUMENT_PATH!);
  if (!documentPath.toLowerCase().endsWith('.docx')) {
    throw new Error('qualification_configuration_invalid:SPINE_E2E_DOCX_DOCUMENT_PATH');
  }

  const accessToken = required('SPINE_LOCAL_AUTH_BEARER_CREDENTIAL');
  const identityVersion = required('SPINE_LOCAL_AUTH_CONFIGURATION_VERSION');
  const workspaceId = uuid('SPINE_WORKSPACE_A_ID');
  const environmentId = uuid('SPINE_ENVIRONMENT_A_ID');
  await installRuntimeBinding(page, { accessToken, environmentId, identityVersion, workspaceId });

  const documentListResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/v1/documents',
  );
  await page.goto(scoped('/knowledge/documents', workspaceId, environmentId));
  if ((await documentListResponse).status() !== 200) throw new Error('documents_page_unavailable');

  await page.locator('input[type="file"]').setInputFiles(documentPath);
  await visible(page.getByRole('region', { name: 'Проверка перед отправкой' }), 'upload_review_unavailable');
  const uploadResponsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/v1/documents/batches',
  );
  await page.getByRole('button', { name: 'Загрузить документы' }).click();
  const uploadResponse = await uploadResponsePromise;
  if (![200, 201, 202].includes(uploadResponse.status())) throw new Error('docx_upload_failed');
  const uploadBody: unknown = await uploadResponse.json();
  const sourceObjectId = firstSourceObjectId(uploadBody);

  await visible(page.getByRole('listitem').filter({ hasText: 'Готов' }), 'docx_processing_not_completed', 180_000);
  await page.goto(scoped(
    `/knowledge/documents/${sourceObjectId}?tab=projection&projection_kind=qa`,
    workspaceId,
    environmentId,
  ));
  const projection = page.getByRole('region', { name: 'Состояние публикации проекции' });
  await visible(projection, 'docx_projection_status_unavailable', 180_000);
  await waitFor(async () => projection.evaluate((element) => {
    const value = element.textContent ?? '';
    return value.includes('Опубликован') && value.includes('Здоров') && value.includes('Актуальна');
  }), 'docx_projection_not_current', 1_800_000);
});

async function installRuntimeBinding(
  page: Page,
  config: { accessToken: string; environmentId: string; identityVersion: string; workspaceId: string },
): Promise<void> {
  await page.addInitScript(({ accessToken, environmentId, identityVersion, workspaceId }) => {
    window.__SPINE_RUNTIME__ = {
      authentication: {
        getBearerCredential: () => accessToken,
        identityVersion,
        role: 'administrator',
        status: 'authenticated',
      },
      scope: {
        defaultEnvironmentId: environmentId,
        defaultWorkspaceId: workspaceId,
        environments: [{ id: environmentId, label: 'Qualification' }],
        workspaces: [{ id: workspaceId, label: 'Workspace' }],
      },
    };
  }, config);
}

function firstSourceObjectId(value: unknown): string {
  if (!isRecord(value) || !Array.isArray(value.items) || !isRecord(value.items[0])) {
    throw new Error('docx_upload_metadata_unavailable');
  }
  const sourceObjectId = value.items[0].source_object_id;
  if (typeof sourceObjectId !== 'string' || !UUID_PATTERN.test(sourceObjectId)) {
    throw new Error('docx_upload_metadata_unavailable');
  }
  return sourceObjectId;
}

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`qualification_configuration_missing:${name}`);
  return value;
}

function uuid(name: string): string {
  const value = required(name);
  if (!UUID_PATTERN.test(value)) throw new Error(`qualification_configuration_invalid:${name}`);
  return value;
}

function scoped(path: string, workspaceId: string, environmentId: string): string {
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}workspace=${workspaceId}&environment=${environmentId}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

async function visible(locator: Locator, reason: string, timeout = 30_000): Promise<void> {
  try {
    await locator.waitFor({ state: 'visible', timeout });
  } catch {
    throw new Error(reason);
  }
}

async function waitFor(
  predicate: () => boolean | Promise<boolean>,
  reason: string,
  timeout: number,
): Promise<void> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
  }
  throw new Error(reason);
}
