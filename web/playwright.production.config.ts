import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { loadEnv } from 'vite';

const repositoryRoot = fileURLToPath(new URL('..', import.meta.url));
const fileEnvironment = loadEnv('qualification', repositoryRoot, '');
for (const [name, value] of Object.entries(fileEnvironment)) {
  if (process.env[name] === undefined) process.env[name] = value;
}

const baseURL = process.env.SPINE_E2E_WEB_BASE_URL;
if (!baseURL) throw new Error('qualification_configuration_missing:SPINE_E2E_WEB_BASE_URL');
const deployedOrigin = new URL(baseURL);
if (deployedOrigin.origin !== baseURL.replace(/\/$/, '') || deployedOrigin.username || deployedOrigin.password) {
  throw new Error('qualification_configuration_invalid:SPINE_E2E_WEB_BASE_URL');
}

export default defineConfig({
  testDir: './e2e/production',
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: 'line',
  preserveOutput: 'never',
  use: {
    baseURL: deployedOrigin.origin,
    screenshot: 'off',
    trace: 'off',
    video: 'off',
  },
  projects: [{ name: 'chromium-production', use: { ...devices['Desktop Chrome'] } }],
});
