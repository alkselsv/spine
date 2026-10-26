import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';

import { AuthProvider, type AuthState } from './app/auth';
import { readBrowserQualificationFixture } from './app/fixture';
import { readBrowserRuntime } from './app/runtime';
import { BrowserRuntimeProvider } from './app/runtime-context';
import { applyTheme, resolveInitialTheme } from './app/theme';
import SpineApp from './ported/spine/SpineApp.jsx';
import './styles/globals.css';
import './ported/index.css';

applyTheme(resolveInitialTheme());

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
  },
});

const rootElement = document.getElementById('root');
const runtime = readBrowserRuntime();

if (!rootElement) {
  throw new Error('Spine web application root element is missing');
}
const applicationRoot = rootElement;

const application = (content: ReactNode, auth = runtime.auth) => (
  <BrowserRuntimeProvider value={runtime}>
    <AuthProvider value={auth}>
      <QueryClientProvider client={queryClient}>{content}</QueryClientProvider>
    </AuthProvider>
  </BrowserRuntimeProvider>
);

async function renderApplication(): Promise<void> {
  const fixture = readBrowserQualificationFixture(__SPINE_FIXTURES_ENABLED__);
  if (__SPINE_FIXTURES_ENABLED__) {
    const { installBrowserQualificationFixtures } = await import('./app/browser-fixtures');
    installBrowserQualificationFixtures(fixture, runtime.auth.status === 'authenticated');
  }

  const fixtureAuth: AuthState =
    fixture === 'auth-denied'
      ? { status: 'forbidden' }
      : runtime.auth.status === 'authenticated'
        ? runtime.auth
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
          };

  createRoot(applicationRoot).render(
    <StrictMode>{application(<SpineApp />, fixture ? fixtureAuth : runtime.auth)}</StrictMode>,
  );
}

void renderApplication();
