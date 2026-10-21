import { describe, expect, it } from 'vitest';

import { SpineApiError } from '../api/client';
import { localizeApiError } from './errors';

describe('structured API error presentation', () => {
  it.each([
    ['spine.authentication.invalid', 'Сеанс истёк', 'authentication'],
    ['spine.authorization.denied', 'Доступ запрещён', 'permissionDenied'],
    ['spine.request.validation', 'Проверьте данные', 'validationFailed'],
    ['spine.client.api_unavailable', 'Сервис временно недоступен', 'degraded'],
  ] as const)('test_%s_maps_to_safe_localized_copy', (code, title, state) => {
    const result = localizeApiError(
      new SpineApiError({
        code,
        kind:
          state === 'authentication'
            ? 'authentication'
            : state === 'permissionDenied'
              ? 'authorization'
              : state === 'validationFailed'
                ? 'validation'
                : state === 'degraded'
                  ? 'unavailable'
                  : 'request',
        retryability: 'never',
        safeMessage: 'raw provider detail must not become product copy',
        status: 503,
        traceId: '50000000-0000-4000-8000-000000000001',
      }),
    );

    expect(result).toMatchObject({ state, title });
    expect(result.description).not.toContain('raw provider');
    expect(result.traceId).toBe('50000000-0000-4000-8000-000000000001');
  });

  it('test_disclosure_safe_not_found_maps_to_the_same_permission_denied_copy', () => {
    const result = localizeApiError(
      new SpineApiError({
        code: 'spine.qa.session.not_found',
        kind: 'request',
        retryability: 'never',
        safeMessage: 'resource state must remain undisclosed',
        status: 404,
        traceId: '50000000-0000-4000-8000-000000000002',
      }),
    );

    expect(result).toMatchObject({
      state: 'permissionDenied',
      title: 'Доступ запрещён',
    });
    expect(result.description).not.toContain('resource');
  });
});
