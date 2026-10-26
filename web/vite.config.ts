import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig(({ mode }) => ({
  define: {
    __SPINE_QUALIFICATION_FIXTURES_ENABLED__: JSON.stringify(
      mode === 'qualification' || mode === 'fixtures',
    ),
    __SPINE_EMBEDDED_FIXTURES_ENABLED__: JSON.stringify(mode === 'fixtures'),
  },
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: './vitest.setup.ts',
    css: true,
    include: ['./src/**/*.test.{ts,tsx}'],
  },
}));
