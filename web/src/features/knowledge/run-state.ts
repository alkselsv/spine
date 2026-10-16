import type { components } from '../../../api/generated/schema';

export type IngestionItemDisplayState =
  | 'uploaded'
  | 'parsing'
  | 'indexing'
  | 'ready'
  | 'rejected'
  | 'failed'
  | 'cancelled'
  | 'unsupported'
  | 'partial';

type IngestionItem =
  | components['schemas']['BatchIngestionItemResponse']
  | components['schemas']['DocumentProcessingHistoryResponse'];

export function mapIngestionItemState(item: IngestionItem): IngestionItemDisplayState {
  if (item.status === 'cancelled' || item.processing === 'cancelled') return 'cancelled';
  if (item.status === 'unsupported' || item.processing === 'unsupported') return 'unsupported';
  if (
    item.status === 'rejected' ||
    item.status === 'quarantined' ||
    item.acceptance === 'rejected' ||
    item.acceptance === 'quarantined'
  ) {
    return 'rejected';
  }
  if (item.status === 'failed' || item.processing === 'failed') return 'failed';
  if (item.coverage === 'failed') return 'failed';
  if (item.status === 'partial' || item.processing === 'partial' || item.coverage === 'partial') {
    return 'partial';
  }
  if (item.item_state === 'projection_requested') return 'indexing';
  if (item.processing === 'processing' || item.item_state === 'processing') return 'parsing';
  if (item.terminal && item.status === 'succeeded' && item.processing === 'succeeded') {
    return 'ready';
  }
  return 'uploaded';
}
