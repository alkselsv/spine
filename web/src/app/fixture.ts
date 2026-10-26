export type BrowserQualificationFixture = 'controlled' | 'chat-api' | 'auth-denied';

/**
 * The fixture build selects its default dataset from the product route. The
 * query parameter remains an explicit override for qualification-only states.
 * Fixtures are never used as authorization data.
 */
export function readBrowserQualificationFixture(
  fixturesEnabled = true,
): BrowserQualificationFixture | null {
  if (!fixturesEnabled || typeof window === 'undefined') return null;
  const value = new URLSearchParams(window.location.search).get('fixture');
  if (value === 'controlled' || value === 'chat-api' || value === 'auth-denied') return value;
  return window.location.pathname.startsWith('/chat') ? 'chat-api' : 'controlled';
}
