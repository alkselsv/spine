import { defineConfig, devices } from '@playwright/test';

const localBypass = '127.0.0.1,localhost';
process.env.NO_PROXY = [localBypass, process.env.NO_PROXY].filter(Boolean).join(',');
process.env.no_proxy = [localBypass, process.env.no_proxy].filter(Boolean).join(',');

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:4175',
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'corepack pnpm exec vite preview --host 127.0.0.1 --port 4175',
    url: 'http://127.0.0.1:4175',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
