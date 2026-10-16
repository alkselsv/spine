import { ru } from '../../i18n/resources';

export function formatDate(value: string | null | undefined) {
  if (!value) return ru.documents.unknown;
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return ru.documents.unknown;
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}
