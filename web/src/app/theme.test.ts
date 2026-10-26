import { afterEach, describe, expect, it, vi } from 'vitest';

import { resolveInitialTheme, THEME_STORAGE_KEY } from './theme';

function stubSystemTheme(prefersDark: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: prefersDark && query === '(prefers-color-scheme: dark)',
    media: query,
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('initial theme', () => {
  it.each([
    [false, 'light'],
    [true, 'dark'],
  ] as const)('test_without_stored_choice_system_preference_dark_%s_selects_%s', (prefersDark, expected) => {
    stubSystemTheme(prefersDark);

    expect(resolveInitialTheme()).toBe(expected);
  });

  it('test_stored_choice_overrides_system_preference', () => {
    stubSystemTheme(true);
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light');

    expect(resolveInitialTheme()).toBe('light');
  });

  it('test_unknown_stored_value_falls_back_to_system_preference', () => {
    stubSystemTheme(true);
    window.localStorage.setItem(THEME_STORAGE_KEY, 'sepia');

    expect(resolveInitialTheme()).toBe('dark');
  });
});
