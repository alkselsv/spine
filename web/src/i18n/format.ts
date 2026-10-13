const russian = 'ru-RU';

export function formatDate(value: Date): string {
  return new Intl.DateTimeFormat(russian, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  }).format(value);
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat(russian).format(value);
}

export function formatDuration(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.round(milliseconds / 1_000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) return `${seconds} с`;
  return `${minutes} мин ${seconds} с`;
}

export function formatBytes(bytes: number): string {
  const safeBytes = Math.max(0, bytes);
  const units = ['Б', 'КиБ', 'МиБ', 'ГиБ'] as const;
  const unitIndex = Math.min(
    Math.floor(Math.log(safeBytes || 1) / Math.log(1_024)),
    units.length - 1,
  );
  const value = safeBytes / 1_024 ** unitIndex;
  const formatted = new Intl.NumberFormat(russian, {
    maximumFractionDigits: unitIndex === 0 ? 0 : 1,
  }).format(value);
  return `${formatted}\u00a0${units[unitIndex]}`;
}
