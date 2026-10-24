export const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const SAFE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/;
export const SAFE_CODE_PATTERN = /^[a-z][a-z0-9_.:-]{0,63}$/;
export const COMMIT_SHA_PATTERN = /^[a-f0-9]{40}$/;
export const DIGEST_PATTERN = /^[a-f0-9]{64}$/;

export function requireSafeUuid(value: unknown, reason: string): string {
  if (typeof value !== 'string' || !UUID_PATTERN.test(value)) throw new Error(reason);
  return value;
}

export function requireSafeDigest(value: unknown, reason: string): string {
  if (typeof value !== 'string' || !DIGEST_PATTERN.test(value)) throw new Error(reason);
  return value;
}

export function requireSafeCode(value: unknown, reason: string): string {
  if (typeof value !== 'string' || !SAFE_CODE_PATTERN.test(value)) throw new Error(reason);
  return value;
}
