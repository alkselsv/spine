import { describe, expect, it } from 'vitest';

import type { components } from '../../../api/generated/schema';
import {
  DEFAULT_UPLOAD_LIMITS,
  applyBatchItems,
  createUploadRows,
  mapBatchItemState,
  validateUploadFiles,
} from './upload';

function file(name: string, size: number, type = 'text/plain'): File {
  return new File([new Uint8Array(size)], name, { type, lastModified: 1_700_000_000_000 });
}

describe('document batch upload contract', () => {
  it('test_invalid_files_are_rejected_before_submission_as_one_batch', () => {
    const files = [file('notes.pdf', 10, 'application/pdf'), file('large.txt', DEFAULT_UPLOAD_LIMITS.maxFileBytes + 1)];

    expect(validateUploadFiles(files)).toEqual({
      valid: false,
      issues: [
        { code: 'unsupported_type', filename: 'notes.pdf' },
        { code: 'file_too_large', filename: 'large.txt' },
      ],
    });
  });

  it('test_batch_limits_reject_without_returning_a_partial_file_list', () => {
    const files = Array.from({ length: DEFAULT_UPLOAD_LIMITS.maxFiles + 1 }, (_, index) =>
      file(`document-${index}.txt`, 1),
    );

    const result = validateUploadFiles(files);

    expect(result.valid).toBe(false);
    expect(result.issues).toEqual([{ code: 'too_many_files' }]);
    const batchTooLarge = Array.from({ length: 6 }, (_, index) =>
      file(`batch-${index}.txt`, DEFAULT_UPLOAD_LIMITS.maxFileBytes),
    );
    expect(validateUploadFiles(batchTooLarge)).toEqual({
      valid: false,
      issues: [{ code: 'batch_too_large' }],
    });
  });

  it('test_valid_files_get_stable_rows_and_input_identities', () => {
    const files = [file('notes.txt', 3), file('notes.txt', 4)];

    expect(createUploadRows(files).map(({ id, inputIdentity }) => ({ id, inputIdentity }))).toEqual([
      {
        id: 'upload-1-notes.txt-3-1700000000000',
        inputIdentity: 'file-1',
      },
      {
        id: 'upload-2-notes.txt-4-1700000000000',
        inputIdentity: 'file-2',
      },
    ]);
  });

  it.each([
    ['accepted', 'not_started', 'non_terminal', false, 'accepted'],
    ['accepted', 'processing', 'non_terminal', false, 'processing'],
    ['accepted', 'succeeded', 'succeeded', true, 'ready'],
    ['accepted', 'succeeded', 'succeeded', false, 'accepted'],
    ['accepted', 'failed', 'failed', true, 'failed'],
    ['rejected', 'not_started', 'rejected', true, 'rejected'],
    ['accepted', 'cancelled', 'cancelled', true, 'cancelled'],
  ] as const)('test_server_item_%s_%s_maps_to_%s', (acceptance, processing, status, terminal, expected) => {
    expect(mapBatchItemState(makeItem({ acceptance, processing, status, terminal }))).toBe(expected);
  });

  it('test_projection_requested_remains_processing_even_when_processing_is_succeeded', () => {
    expect(
      mapBatchItemState(
        makeItem({ item_state: 'projection_requested', processing: 'succeeded', status: 'succeeded' }),
      ),
    ).toBe('processing');
  });

  it('test_mixed_server_items_preserve_every_local_row_independently', () => {
    const rows = createUploadRows([file('ok.txt', 1), file('bad.txt', 1)]);
    const updated = applyBatchItems(rows, [
      makeItem({ input_identity: 'file-1', processing: 'succeeded', status: 'succeeded', terminal: true }),
      makeItem({ input_identity: 'file-2', processing: 'failed', status: 'failed', retryable: true }),
    ]);

    expect(updated.map(({ state, retryable }) => ({ state, retryable }))).toEqual([
      { state: 'ready', retryable: false },
      { state: 'failed', retryable: true },
    ]);
  });
});

function makeItem(
  overrides: Partial<components['schemas']['BatchIngestionItemResponse']> = {},
): components['schemas']['BatchIngestionItemResponse'] {
  return {
    acceptance: 'accepted',
    attempt: 1,
    coverage: null,
    failure_ids: [],
    failures: [],
    input_identity: 'file-1',
    input_ordinal: 1,
    item_id: '30000000-0000-4000-8000-000000000001',
    item_state: 'accepted',
    processing: 'not_started',
    processing_receipt: null,
    retryable: false,
    source_object_id: null,
    source_revision_id: null,
    status: 'non_terminal',
    terminal: false,
    warning_ids: [],
    warnings: [],
    ...overrides,
  };
}
