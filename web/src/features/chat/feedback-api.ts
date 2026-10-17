import type { components } from '../../../api/generated/schema';
import type { ScopedApiClient } from '../../api/client';

export type QaFeedbackRequest = components['schemas']['SubmitFeedbackRequest'];
export type QaFeedbackReceipt = components['schemas']['FeedbackResponse'];

export async function submitQaFeedback(
  client: ScopedApiClient,
  command: {
    request: QaFeedbackRequest;
    idempotencyKey: string;
    signal: AbortSignal;
  },
): Promise<QaFeedbackReceipt> {
  const response = await client.POST('/api/v1/qa/feedback', {
    params: { header: { 'Idempotency-Key': command.idempotencyKey } },
    body: command.request,
    signal: command.signal,
  });
  const receipt = response.data?.result;
  if (
    !receipt ||
    receipt.schema_version !== 1 ||
    receipt.status !== 'recorded' ||
    receipt.feedback_version !== 1 ||
    receipt.target_kind !== command.request.target_kind ||
    receipt.target_id !== command.request.target_id ||
    receipt.outcome !== command.request.outcome ||
    receipt.run_id !== command.request.run_id ||
    (receipt.message_id ?? null) !== (command.request.message_id ?? null) ||
    (receipt.citation_id ?? null) !== (command.request.citation_id ?? null)
  ) {
    throw new Error('Feedback is unavailable.');
  }
  return receipt;
}
