import { defineConfig, devices } from '@playwright/test';

const previewPort = Number(process.env.SPINE_CONTROLLED_PREVIEW_PORT ?? '4175');
const apiPort = Number(process.env.SPINE_ISSUE646_API_PORT ?? '4176');
const previewOrigin = `http://127.0.0.1:${previewPort}`;
const apiOrigin = `http://127.0.0.1:${apiPort}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: 'app.spec.ts',
  forbidOnly: Boolean(process.env.CI),
  reporter: 'line',
  grep: /@requirements-composed/,
  use: {
    baseURL: previewOrigin,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `SPINE_QUALIFICATION_API_ORIGIN=${apiOrigin} corepack pnpm exec vite preview --host 127.0.0.1 --port ${previewPort} --strictPort`,
      url: previewOrigin,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `SPINE_ISSUE646_API_PORT=${apiPort} uv run --directory .. python -m tests.qualification.issue646_server`,
      url: `${apiOrigin}/api/v1/health/live`,
      reuseExistingServer: false,
      timeout: 180_000,
    },
  ],
});
