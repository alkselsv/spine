import type { components } from '../../../api/generated/schema';

export const DEFAULT_UPLOAD_LIMITS = {
  maxFiles: 20,
  maxFileBytes: 10_000_000,
  maxBatchBytes: 50_000_000,
} as const;

const DOCX_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const XLSX_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const SUPPORTED_FILE_TYPES = new Map<string, ReadonlySet<string>>([
  ['.docx', new Set([DOCX_MEDIA_TYPE])],
  ['.md', new Set(['text/markdown', 'text/plain', 'text/x-markdown'])],
  ['.markdown', new Set(['text/markdown', 'text/plain', 'text/x-markdown'])],
  ['.pdf', new Set(['application/pdf'])],
  ['.txt', new Set(['text/plain'])],
  ['.xlsx', new Set([XLSX_MEDIA_TYPE])],
]);
const SUPPORTED_MEDIA_TYPES = new Set(
  Array.from(SUPPORTED_FILE_TYPES.values()).flatMap((mediaTypes) => Array.from(mediaTypes)),
);
export const DOCUMENT_UPLOAD_ACCEPT = [
  ...SUPPORTED_FILE_TYPES.keys(),
  ...SUPPORTED_MEDIA_TYPES,
].join(',');
const MULTIPART_OVERHEAD_RESERVE_PER_FILE = 1_024;

export type UploadValidationIssueCode =
  | 'empty_batch'
  | 'too_many_files'
  | 'batch_too_large'
  | 'unsupported_type'
  | 'file_too_large';

export interface UploadValidationIssue {
  code: UploadValidationIssueCode;
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
  const estimatedRequestBytes = totalBytes + files.length * MULTIPART_OVERHEAD_RESERVE_PER_FILE;
  if (estimatedRequestBytes > limits.maxBatchBytes) issues.push({ code: 'batch_too_large' });

  for (const file of files) {
    if (!isSupportedFile(file)) issues.push({ code: 'unsupported_type' });
    if (file.size > limits.maxFileBytes) issues.push({ code: 'file_too_large' });
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

export function resolveUploadMediaType(file: File): string {
  const declaredMediaType = file.type.trim().toLowerCase();
  if (declaredMediaType) return declaredMediaType;
  const extension = uploadExtension(file.name);
  const canonicalMediaType = SUPPORTED_FILE_TYPES.get(extension)?.values().next().value;
  return canonicalMediaType ?? 'application/octet-stream';
}

export function mapBatchItemState(
  item: components['schemas']['BatchIngestionItemResponse'],
): UploadRowState {
  if (item.status === 'cancelled' || item.processing === 'cancelled') return 'cancelled';
  if (item.acceptance === 'rejected' || item.acceptance === 'quarantined' || item.status === 'rejected' || item.status === 'unsupported') {
    return 'rejected';
  }
  if (item.status === 'failed' || item.processing === 'failed') {
    return 'failed';
  }
  if (item.status === 'partial' || item.processing === 'partial') return 'failed';
  if (item.item_state === 'projection_requested') return 'processing';
  if (item.terminal && item.processing === 'succeeded' && item.status === 'succeeded') return 'ready';
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
  const extension = uploadExtension(file.name);
  const mediaType = file.type.trim().toLowerCase();
  if (!extension) return false;
  const allowedMediaTypes = SUPPORTED_FILE_TYPES.get(extension);
  if (!allowedMediaTypes) return false;
  return !mediaType || allowedMediaTypes.has(mediaType);
}

function uploadExtension(filename: string): string {
  const dot = filename.lastIndexOf('.');
  return dot > 0 ? filename.slice(dot).toLowerCase() : '';
}
