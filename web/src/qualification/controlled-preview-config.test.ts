import { afterEach, describe, expect, test, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

async function loadConfig(port?: string) {
  vi.resetModules();
  if (port === undefined) {
    vi.stubEnv('SPINE_CONTROLLED_PREVIEW_PORT', '');
    delete process.env.SPINE_CONTROLLED_PREVIEW_PORT;
  } else {
    vi.stubEnv('SPINE_CONTROLLED_PREVIEW_PORT', port);
  }
  return (await import('../../playwright.config')).default;
}

describe('controlled browser preview configuration', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  test('test_default_preview_uses_one_owned_strict_address', async () => {
    const config = await loadConfig();

    expect(config.use?.baseURL).toBe('http://127.0.0.1:4175');
    expect(config.webServer).toMatchObject({
      command: 'corepack pnpm exec vite preview --host 127.0.0.1 --port 4175 --strictPort',
      reuseExistingServer: false,
      url: 'http://127.0.0.1:4175',
    });
  });

  test('test_configured_preview_port_drives_browser_and_server_address', async () => {
    const config = await loadConfig('43127');

    expect(config.use?.baseURL).toBe('http://127.0.0.1:43127');
    expect(config.webServer).toMatchObject({
      command: 'corepack pnpm exec vite preview --host 127.0.0.1 --port 43127 --strictPort',
      reuseExistingServer: false,
      url: 'http://127.0.0.1:43127',
    });
  });

  test.each(['not-a-port', '0', '65536', '4175.5'])(
    'test_invalid_preview_port_is_rejected_before_browser_execution:%s',
    async (port) => {
      await expect(loadConfig(port)).rejects.toThrow(
        'qualification_configuration_invalid:SPINE_CONTROLLED_PREVIEW_PORT',
      );
    },
  );

  test('test_controlled_e2e_command_builds_current_sources_before_playwright', () => {
    const packageManifest = JSON.parse(
      readFileSync(resolve(process.cwd(), 'package.json'), 'utf8'),
    ) as { scripts: Record<string, string> };

    expect(packageManifest.scripts['test:e2e:run']).toBe(
      'corepack pnpm test:e2e:qualification && corepack pnpm test:e2e:fixtures',
    );
    expect(packageManifest.scripts['test:e2e:qualification']).toBe(
      'corepack pnpm build:qualification && playwright test --config playwright.config.ts --grep-invert @embedded-fixture',
    );
    expect(packageManifest.scripts['test:e2e:fixtures']).toBe(
      'corepack pnpm build:fixtures && playwright test --config playwright.config.ts --grep @embedded-fixture',
    );
    expect(packageManifest.scripts['test:e2e']).toBe(
      'corepack pnpm test:e2e:ownership && corepack pnpm test:e2e:run',
    );
  });
});
