import type { components, paths } from '../../../api/generated/schema';

type AdmissionPost = paths['/api/v1/ask/admissions']['post'];
type AdmissionRequest = NonNullable<AdmissionPost['requestBody']>['content']['application/json'];
type AdmissionResponse = NonNullable<
  AdmissionPost['responses'][202]['content']
>['application/json'];
type CancelPost = paths['/api/v1/ask/{run_id}/cancel']['post'];
type CancelRequest = NonNullable<CancelPost['requestBody']>['content']['application/json'];
type CancelResponse = NonNullable<
  CancelPost['responses'][202]['content']
>['application/json'];

export const durableAskAdmissionRequest = {
  schema_version: 1,
  session_id: '10000000-0000-0000-0000-000000000430',
  question: 'Where is the current policy?',
} satisfies AdmissionRequest;

export const durableAskAdmissionResponse = {
  schema_version: 1,
  command_id: '10000000-0000-0000-0000-000000000431',
  workspace_id: '20000000-0000-0000-0000-000000000430',
  environment_id: '30000000-0000-0000-0000-000000000430',
  trace_id: '40000000-0000-0000-0000-000000000430',
  status: 'accepted',
  replay: false,
  result: {
    schema_version: 1,
    command_id: '10000000-0000-0000-0000-000000000431',
    session_id: durableAskAdmissionRequest.session_id,
    message_id: '10000000-0000-0000-0000-000000000432',
    run_id: '10000000-0000-0000-0000-000000000433',
    status: 'admitted',
  },
} satisfies AdmissionResponse;

export const durableAskCancelRequest = {
  schema_version: 1,
} satisfies CancelRequest;

export const durableAskCancelResponse = {
  schema_version: 1,
  command_id: durableAskAdmissionResponse.command_id,
  workspace_id: '20000000-0000-0000-0000-000000000430',
  environment_id: '30000000-0000-0000-0000-000000000430',
  trace_id: '40000000-0000-0000-0000-000000000430',
  status: 'accepted',
  replay: false,
  result: {
    schema_version: 1,
    command_id: durableAskAdmissionResponse.result.command_id,
    run_id: durableAskAdmissionResponse.result.run_id,
    status: 'cancellation_requested',
  },
} satisfies CancelResponse;

export const durableAskTerminalResult: components['schemas']['QAAskAdmissionResultV1'] = {
  ...durableAskAdmissionResponse.result,
  status: 'terminal',
  outcome: 'abstained',
};
