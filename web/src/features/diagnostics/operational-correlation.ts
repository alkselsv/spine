import type { components, paths } from '../../../api/generated/schema';
import type { ApiScope, ScopedApiClient } from '../../api/client';

export type CorrelationReferenceKind = components['schemas']['CorrelationReferenceKind'];
export type OperationalCorrelationView = components['schemas']['OperationalCorrelationView'];

export interface OperationalCorrelationResult {
  authorizationAuditEventId: string;
  requestTraceId: string;
  view: OperationalCorrelationView;
}

export interface OperationalCorrelationRequest {
  referenceId: string;
  referenceKind: CorrelationReferenceKind;
  scope: ApiScope;
}

type CorrelationResponse =
  paths['/api/v1/diagnostics/operational-correlation']['get']['responses'][200]['content']['application/json'];

export async function loadOperationalCorrelation(
  client: ScopedApiClient,
  request: OperationalCorrelationRequest,
  signal: AbortSignal,
): Promise<OperationalCorrelationResult> {
  const result = await client.GET('/api/v1/diagnostics/operational-correlation', {
    params: {
      query: {
        reference_kind: request.referenceKind,
        reference_id: request.referenceId,
      },
    },
    signal,
  });
  if (!result.data) throw new Error('Operational correlation response is unavailable.');

  const response: CorrelationResponse = result.data;
  const view = response.data;
  if (response.schema_version !== 1 || view.schema_version !== 'operational-correlation-view:v1') {
    throw new Error('Operational correlation response version is not supported.');
  }
  if (!isUuid(response.authorization_audit_event_id) || !isUuid(response.trace_id)) {
    throw new Error('Operational correlation response references are invalid.');
  }
  if (
    response.workspace_id !== request.scope.workspaceId ||
    response.environment_id !== request.scope.environmentId ||
    view.workspace_id !== request.scope.workspaceId ||
    view.environment_id !== request.scope.environmentId ||
    view.records.some(
      (record) =>
        record.workspace_id !== request.scope.workspaceId ||
        record.environment_id !== request.scope.environmentId,
    )
  ) {
    throw new Error('Operational correlation response is outside the selected scope.');
  }
  if (view.reference_kind !== request.referenceKind || view.reference_id !== request.referenceId) {
    throw new Error('Operational correlation response does not match the requested reference.');
  }
  return {
    authorizationAuditEventId: response.authorization_audit_event_id,
    requestTraceId: response.trace_id,
    view,
  };
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
