import { describe, expect, it } from 'vitest';

import {
  durableAskAdmissionRequest,
  durableAskAdmissionResponse,
  durableAskCancelRequest,
  durableAskCancelResponse,
  durableAskTerminalResult,
} from './durable-ask-api-contract';

describe('generated durable Ask API contract', () => {
  it('keeps admission and cancellation examples aligned with generated OpenAPI types', () => {
    expect(durableAskAdmissionRequest.schema_version).toBe(1);
    expect(durableAskAdmissionResponse.result?.status).toBe('admitted');
    expect(durableAskCancelRequest.schema_version).toBe(1);
    expect(durableAskCancelResponse.result?.status).toBe('cancellation_requested');
    expect(durableAskTerminalResult.status).toBe('terminal');
  });
});
