import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['./e2e/controlled-preview-command.test.ts'],
  },
});
