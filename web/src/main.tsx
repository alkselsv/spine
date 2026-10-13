import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router';

import { AuthProvider } from './app/auth';
import { readBrowserQualificationFixture } from './app/fixture';
import { appRoutes } from './app/routes';
import './styles/globals.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
  },
});

const router = createBrowserRouter(appRoutes);
const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error('Spine web application root element is missing');
}

createRoot(rootElement).render(
  <StrictMode>
    <AuthProvider
      value={
        readBrowserQualificationFixture() === 'auth-denied'
          ? { status: 'forbidden' }
          : {
              status: 'authenticated',
              role: 'administrator',
              ...(readBrowserQualificationFixture() === 'controlled'
                ? { getBearerCredential: () => 'controlled-browser-token' }
                : {}),
            }
      }
    >
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </AuthProvider>
  </StrictMode>,
);
