import { defineConfig, devices } from '@playwright/test';

const localBypass = '127.0.0.1,localhost';
process.env.NO_PROXY = [localBypass, process.env.NO_PROXY].filter(Boolean).join(',');
process.env.no_proxy = [localBypass, process.env.no_proxy].filter(Boolean).join(',');

const previewPortValue = process.env.SPINE_CONTROLLED_PREVIEW_PORT ?? '4175';
if (!/^\d+$/.test(previewPortValue)) {
  throw new Error('qualification_configuration_invalid:SPINE_CONTROLLED_PREVIEW_PORT');
}
const previewPort = Number(previewPortValue);
if (!Number.isSafeInteger(previewPort) || previewPort < 1 || previewPort > 65_535) {
  throw new Error('qualification_configuration_invalid:SPINE_CONTROLLED_PREVIEW_PORT');
}
const previewOrigin = `http://127.0.0.1:${previewPort}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: 'app.spec.ts',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: 'line',
  use: {
    baseURL: previewOrigin,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `corepack pnpm exec vite preview --host 127.0.0.1 --port ${previewPort} --strictPort`,
    url: previewOrigin,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
