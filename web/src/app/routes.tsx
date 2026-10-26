import type { RouteObject } from 'react-router';

import SpineApp from '../ported/spine/SpineApp';
import { AdministratorBoundary } from './auth';

export { ApiErrorPanel, StatusPanel } from './status';
export type { InterfaceState } from './status';

function SpineControlPlane() {
  return (
    <AdministratorBoundary>
      <SpineApp />
    </AdministratorBoundary>
  );
}

export const appRoutes: RouteObject[] = [
  {
    path: '/knowledge/documents/:documentId',
    element: <SpineControlPlane />,
  },
  {
    path: '/runs/:runId',
    element: <SpineControlPlane />,
  },
  {
    path: '*',
    element: <SpineControlPlane />,
  },
];
