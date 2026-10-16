import type { components } from '../../../api/generated/schema';

export const DEFAULT_UPLOAD_LIMITS = {
  maxFiles: 20,
  maxFileBytes: 10_000_000,
  maxBatchBytes: 50_000_000,
} as const;

const SUPPORTED_EXTENSIONS = new Set(['.md', '.markdown', '.txt']);
const SUPPORTED_MEDIA_TYPES = new Set(['text/markdown', 'text/plain']);

export type UploadValidationIssueCode =
  | 'empty_batch'
  | 'too_many_files'
  | 'batch_too_large'
  | 'unsupported_type'
  | 'file_too_large';

export interface UploadValidationIssue {
  code: UploadValidationIssueCode;
  filename?: string;
}

export interface UploadValidationResult {
  valid: boolean;
  issues: UploadValidationIssue[];
}

export type UploadRowState =
  | 'queued'
  | 'uploading'
  | 'accepted'
  | 'processing'
  | 'ready'
  | 'rejected'
  | 'failed'
  | 'cancelled';

export interface UploadRow {
  id: string;
  inputIdentity: string;
  file: File;
  state: UploadRowState;
  retryable: boolean;
  serverItem?: components['schemas']['BatchIngestionItemResponse'];
}

export function validateUploadFiles(
  files: readonly File[],
  limits: typeof DEFAULT_UPLOAD_LIMITS = DEFAULT_UPLOAD_LIMITS,
): UploadValidationResult {
  if (files.length === 0) return { valid: false, issues: [{ code: 'empty_batch' }] };

  const issues: UploadValidationIssue[] = [];
  if (files.length > limits.maxFiles) issues.push({ code: 'too_many_files' });

  const totalBytes = files.reduce((total, value) => total + value.size, 0);
  if (totalBytes > limits.maxBatchBytes) issues.push({ code: 'batch_too_large' });

  for (const file of files) {
    if (!isSupportedFile(file)) issues.push({ code: 'unsupported_type', filename: file.name });
    if (file.size > limits.maxFileBytes) issues.push({ code: 'file_too_large', filename: file.name });
  }

  return { valid: issues.length === 0, issues };
}

export function createUploadRows(files: readonly File[]): UploadRow[] {
  return files.map((file, index) => ({
    id: `upload-${index + 1}-${file.name}-${file.size}-${file.lastModified}`,
    inputIdentity: `file-${index + 1}`,
    file,
    state: 'queued',
    retryable: false,
  }));
}

export function createIdempotencyKey(): string {
  const crypto = globalThis.crypto;
  if (crypto?.randomUUID) return `batch-upload-${crypto.randomUUID()}`;
  return `batch-upload-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function mapBatchItemState(
  item: components['schemas']['BatchIngestionItemResponse'],
): UploadRowState {
  if (item.status === 'cancelled' || item.processing === 'cancelled') return 'cancelled';
  if (item.acceptance === 'rejected' || item.acceptance === 'quarantined' || item.status === 'rejected' || item.status === 'unsupported') {
    return 'rejected';
  }
  if (item.status === 'failed' || item.processing === 'failed' || item.status === 'partial' || item.processing === 'partial') {
    return 'failed';
  }
  if (item.item_state === 'projection_requested') return 'processing';
  if (item.processing === 'succeeded' && item.status === 'succeeded') return 'ready';
  if (item.processing === 'processing' || item.item_state === 'processing') {
    return 'processing';
  }
  if (item.acceptance === 'accepted') return 'accepted';
  return 'queued';
}

export function applyBatchItems(
  rows: readonly UploadRow[],
  items: readonly components['schemas']['BatchIngestionItemResponse'][],
): UploadRow[] {
  const byIdentity = new Map(items.map((item) => [item.input_identity, item]));
  return rows.map((row) => {
    const item = byIdentity.get(row.inputIdentity);
    if (!item) return row;
    const state = mapBatchItemState(item);
    return {
      ...row,
      state,
      retryable: item.retryable && (state === 'failed' || state === 'rejected'),
      serverItem: item,
    };
  });
}

function isSupportedFile(file: File): boolean {
  const dot = file.name.lastIndexOf('.');
  const extension = dot > 0 ? file.name.slice(dot).toLowerCase() : '';
  const mediaType = file.type.toLowerCase();
  if (extension && !SUPPORTED_EXTENSIONS.has(extension)) return false;
  if (mediaType && !SUPPORTED_MEDIA_TYPES.has(mediaType)) return false;
  return SUPPORTED_EXTENSIONS.has(extension) || SUPPORTED_MEDIA_TYPES.has(mediaType);
}
