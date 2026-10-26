import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router';

import { AuthProvider } from './app/auth';
import { readBrowserQualificationFixture } from './app/fixture';
import { appRoutes } from './app/routes';
import { readBrowserRuntime } from './app/runtime';
import { BrowserRuntimeProvider } from './app/runtime-context';
import { applyTheme, resolveInitialTheme } from './app/theme';
import './styles/globals.css';

applyTheme(resolveInitialTheme());

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
  },
});

const router = createBrowserRouter(appRoutes);
const rootElement = document.getElementById('root');
const runtime = readBrowserRuntime();

if (!rootElement) {
  throw new Error('Spine web application root element is missing');
}
const applicationRoot = rootElement;

async function renderApplication(): Promise<void> {
  const fixture = readBrowserQualificationFixture(
    __SPINE_QUALIFICATION_FIXTURES_ENABLED__,
    __SPINE_EMBEDDED_FIXTURES_ENABLED__,
  );
  if (__SPINE_EMBEDDED_FIXTURES_ENABLED__) {
    const { installBrowserQualificationFixtures } = await import('./app/browser-fixtures');
    installBrowserQualificationFixtures(fixture, runtime.auth.status === 'authenticated');
  }

  createRoot(applicationRoot).render(
    <StrictMode>
      <BrowserRuntimeProvider value={runtime}>
        <AuthProvider
          value={
            fixture === 'auth-denied'
              ? { status: 'forbidden' }
              : {
                  ...runtime.auth,
                  ...(fixture === 'controlled'
                    ? {
                        status: 'authenticated' as const,
                        role: 'administrator' as const,
                        getBearerCredential: () => 'controlled-browser-token',
                        identityVersion: 'controlled-browser-identity',
                      }
                    : {}),
                  ...(fixture === 'chat-api'
                    ? {
                        status: 'authenticated' as const,
                        role: 'administrator' as const,
                        getBearerCredential: () => 'controlled-chat-api-token',
                        identityVersion: 'controlled-chat-api-identity',
                      }
                    : {}),
                }
          }
        >
          <QueryClientProvider client={queryClient}>
            <RouterProvider router={router} />
          </QueryClientProvider>
        </AuthProvider>
      </BrowserRuntimeProvider>
    </StrictMode>,
  );
}

void renderApplication();
