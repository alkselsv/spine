import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';

import { buildCitationPlacements } from './citation-presentation';
import type { QAMessage } from './session-api';

const scope = {
  workspace_id: '10000000-0000-4000-8000-000000000001',
  environment_id: '20000000-0000-4000-8000-000000000001',
};
const messageId = '50000000-0000-4000-8000-000000000001';
const runId = '70000000-0000-4000-8000-000000000001';
const answerId = '80000000-0000-4000-8000-000000000001';
const attemptId = '90000000-0000-4000-8000-000000000001';
const claimId = 'a0000000-0000-4000-8000-000000000001';
const relationshipId = 'b0000000-0000-4000-8000-000000000001';
const citationId = 'c0000000-0000-4000-8000-000000000001';

function acceptedMessage(answer: string, startOffset: number, endOffset: number): QAMessage {
  return {
    schema_version: 1,
    message_id: messageId,
    session_id: '30000000-0000-4000-8000-000000000001',
    ...scope,
    version: 1,
    ordinal: 1,
    trace_id: '60000000-0000-4000-8000-000000000001',
    run_id: runId,
    state: 'terminal',
    terminal_outcome: 'answered',
    cancelled: false,
    disclosure_eligibility: 'eligible',
    question: 'Synthetic question',
    answer,
    accepted_presentation: {
      schema_version: 'accepted-answer-presentation:v1',
      answer_id: answerId,
      answer_text_sha256: createHash('sha256').update(answer, 'utf8').digest('hex'),
      attempt_id: attemptId,
      message_id: messageId,
      run_id: runId,
      claims: [{
        schema_version: 'accepted-answer-claim-presentation:v1',
        claim_id: claimId,
        kind: 'factual',
        start_offset: startOffset,
        end_offset: endOffset,
      }],
      relationships: [{
        schema_version: 'accepted-answer-relationship-presentation:v1',
        relationship_id: relationshipId,
        claim_id: claimId,
        kind: 'independent',
        citation_ids: [citationId],
        related_claim_id: null,
      }],
      citation_ids: [citationId],
    },
    created_at: '2026-10-17T10:00:00Z',
    updated_at: '2026-10-17T10:00:00Z',
  };
}

describe('accepted citation placement', () => {
  it('test_authoritative_codepoint_spans_preserve_exact_unicode_text', async () => {
    const answer = 'Факт 😀 e\u0301 и é.';
    const start = 7;
    const end = 12;

    const [placement] = await buildCitationPlacements(acceptedMessage(answer, start, end)) ?? [];

    expect(placement?.startOffset).toBe(start);
    expect(placement?.endOffset).toBe(end);
    expect(placement?.text).toBe('e\u0301 и ');
    expect(placement?.utf16StartOffset).toBe(8);
    expect(placement?.utf16EndOffset).toBe(13);
    expect(answer.slice(placement?.utf16StartOffset, placement?.utf16EndOffset)).toBe('e\u0301 и ');
  });

  it('test_precomposed_and_decomposed_text_keep_distinct_python_codepoint_boundaries', async () => {
    const answer = 'Факт 😀 e\u0301 и é.';
    const message = acceptedMessage(answer, 7, 12);
    const presentation = message.accepted_presentation!;
    const secondClaimId = 'a0000000-0000-4000-8000-000000000002';
    const secondRelationshipId = 'b0000000-0000-4000-8000-000000000002';
    const secondCitationId = 'c0000000-0000-4000-8000-000000000002';
    presentation.claims.push({
      schema_version: 'accepted-answer-claim-presentation:v1',
      claim_id: secondClaimId,
      kind: 'factual',
      start_offset: 12,
      end_offset: 14,
    });
    presentation.relationships.push({
      schema_version: 'accepted-answer-relationship-presentation:v1',
      relationship_id: secondRelationshipId,
      claim_id: secondClaimId,
      kind: 'independent',
      citation_ids: [secondCitationId],
      related_claim_id: null,
    });
    presentation.citation_ids.push(secondCitationId);

    const placements = await buildCitationPlacements(message);

    expect(placements?.map(({ startOffset, endOffset, text }) => [startOffset, endOffset, text])).toEqual([
      [7, 12, 'e\u0301 и '],
      [12, 14, 'é.'],
    ]);
    expect(answer.slice(placements?.[1]?.utf16StartOffset, placements?.[1]?.utf16EndOffset)).toBe('é.');
    expect(placements?.[1]?.utf16StartOffset).toBe(13);
    expect(placements?.[1]?.utf16EndOffset).toBe(15);
  });

  it('test_mismatched_claim_and_citation_relationship_identities_fail_closed', async () => {
    const invalid = acceptedMessage('Факт.', 0, 5);
    invalid.accepted_presentation!.relationships[0]!.claim_id = 'a0000000-0000-4000-8000-000000000099';

    expect(await buildCitationPlacements(invalid)).toBeNull();
  });

  it('test_ineligible_or_stale_accepted_message_has_no_citation_placements', async () => {
    const ineligible = acceptedMessage('Факт.', 0, 5);
    ineligible.disclosure_eligibility = 'ineligible';

    const staleText = acceptedMessage('Факт.', 0, 5);
    staleText.answer = 'Другой факт.';

    expect(await buildCitationPlacements(ineligible)).toBeNull();
    expect(await buildCitationPlacements(staleText)).toBeNull();
  });
});
