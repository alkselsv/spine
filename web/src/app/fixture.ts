export type BrowserQualificationFixture = 'controlled' | 'auth-denied';

/**
 * Qualification fixtures are selected only by the browser test harness. They
 * stand in for Issue #26 adapters and are never used as authorization data.
 */
export function readBrowserQualificationFixture(): BrowserQualificationFixture | null {
  if (typeof window === 'undefined') return null;
  const value = new URLSearchParams(window.location.search).get('fixture');
  return value === 'controlled' || value === 'auth-denied' ? value : null;
}
