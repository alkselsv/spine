import { afterEach, describe, expect, it } from 'vitest';

import { readBrowserQualificationFixture } from './fixture';

describe('browser fixture build boundary', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('test_live_build_ignores_fixture_query_parameter', () => {
    window.history.replaceState(null, '', '/chat?fixture=chat-api');

    expect(readBrowserQualificationFixture(false)).toBeNull();
  });

  it('test_fixture_build_accepts_supported_fixture_query_parameter', () => {
    window.history.replaceState(null, '', '/chat?fixture=chat-api');

    expect(readBrowserQualificationFixture(true)).toBe('chat-api');
  });

  it('test_fixture_build_selects_chat_fixture_from_route_without_query_parameter', () => {
    window.history.replaceState(null, '', '/chat');

    expect(readBrowserQualificationFixture(true)).toBe('chat-api');
  });

  it.each(['/knowledge/documents', '/qa/runs'])(
    'test_fixture_build_selects_controlled_fixture_from_route_without_query_parameter:%s',
    (path) => {
      window.history.replaceState(null, '', path);

      expect(readBrowserQualificationFixture(true)).toBe('controlled');
    },
  );
});
