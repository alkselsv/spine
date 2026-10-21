import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router';

import { AuthProvider } from './app/auth';
import { readBrowserQualificationFixture } from './app/fixture';
import { appRoutes } from './app/routes';
import { readBrowserRuntime } from './app/runtime';
import { BrowserRuntimeProvider } from './app/runtime-context';
import './styles/globals.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
  },
});

const router = createBrowserRouter(appRoutes);
const rootElement = document.getElementById('root');
const fixture = readBrowserQualificationFixture();
const runtime = readBrowserRuntime();

if (!rootElement) {
  throw new Error('Spine web application root element is missing');
}

createRoot(rootElement).render(
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
