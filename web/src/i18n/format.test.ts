import { describe, expect, it } from 'vitest';

import { formatBytes, formatDate, formatDuration, formatNumber } from './format';

describe('Russian interface formatting', () => {
  it('test_date_uses_russian_intl_format', () => {
    expect(formatDate(new Date('2026-10-13T10:30:00Z'))).toBe('13 окт. 2026 г.');
  });

  it('test_number_uses_russian_grouping', () => {
    expect(formatNumber(1234567)).toBe('1 234 567');
  });

  it('test_duration_uses_localized_units', () => {
    expect(formatDuration(125000)).toBe('2 мин 5 с');
  });

  it('test_size_uses_localized_iec_units', () => {
    expect(formatBytes(1536)).toBe('1,5 КиБ');
  });
});
