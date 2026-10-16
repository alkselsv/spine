import { SpineApiError } from '../api/client';
import { ru } from '../i18n/resources';

export type ProductErrorState =
  | 'authentication'
  | 'permissionDenied'
  | 'conflict'
  | 'validationFailed'
  | 'degraded'
  | 'failed';

export interface LocalizedApiError {
  state: ProductErrorState;
  title: string;
  description: string;
  traceId: string | null;
}

export function localizeApiError(error: unknown): LocalizedApiError {
  if (!(error instanceof SpineApiError)) {
    return {
      state: 'failed',
      ...ru.errors.failed,
      traceId: null,
    };
  }

  if (error.kind === 'authentication') {
    return {
      state: 'authentication',
      ...ru.errors.authentication,
      traceId: error.traceId,
    };
  }
  if (error.kind === 'authorization') {
    return {
      state: 'permissionDenied',
      ...ru.errors.authorization,
      traceId: error.traceId,
    };
  }
  if (error.status === 409) {
    return {
      state: 'conflict',
      ...ru.errors.conflict,
      traceId: error.traceId,
    };
  }
  if (error.kind === 'validation') {
    return {
      state: 'validationFailed',
      ...ru.errors.validation,
      traceId: error.traceId,
    };
  }
  if (error.kind === 'unavailable') {
    return {
      state: 'degraded',
      ...ru.errors.unavailable,
      traceId: error.traceId,
    };
  }
  return {
    state: 'failed',
    ...ru.errors.failed,
    traceId: error.traceId,
  };
}
