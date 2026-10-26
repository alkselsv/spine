import { describe, expect, it } from 'vitest';

import { mapApiMessage } from './spine/chat/useSpineChat.js';
import { normalizeApiDocument } from './spine/useSpineDocuments.js';

const id = (tail: string) => `00000000-0000-4000-8000-${tail.padStart(12, '0')}`;

describe('ported UI API adapters', () => {
  it('test_document_snapshot_preserves_server_state_for_the_new_knowledge_ui', () => {
    const revision = {
      source_revision_id: id('2'),
      lifecycle: 'current',
      observed_at: '2026-10-26T10:00:00Z',
    };
    const snapshot = {
      schema_version: 1,
      source_object_id: id('1'),
      source_kind: 'document',
      created_at: '2026-10-26T09:00:00Z',
      current_revision: revision,
      operational_metadata: { status: 'available', processing_state: 'processed' },
      publication: { status: 'published', health: 'healthy' },
    };

    const mapped = normalizeApiDocument(snapshot);

    expect(mapped.source_object_id).toBe(snapshot.source_object_id);
    expect(mapped.operational_metadata).toBe(snapshot.operational_metadata);
    expect(mapped.revisions).toEqual([revision]);
    expect(mapped.segments).toEqual([]);
  });

  it('test_accepted_answer_maps_server_claims_and_citations_without_demo_data', () => {
    const claimId = id('11');
    const citationId = id('12');
    const message = {
      schema_version: 1,
      workspace_id: id('20'),
      environment_id: id('21'),
      session_id: id('22'),
      message_id: id('23'),
      run_id: id('24'),
      ordinal: 1,
      created_at: '2026-10-26T10:00:00Z',
      updated_at: '2026-10-26T10:00:02Z',
      question: 'Какой срок?',
      answer: 'Срок — 36 месяцев.',
      state: 'terminal',
      terminal_outcome: 'answered',
      cancelled: false,
      disclosure_eligibility: 'eligible',
      trace_id: id('25'),
      version: 3,
      completeness: null,
      accepted_presentation: {
        schema_version: 'accepted-answer-presentation:v1',
        answer_id: id('26'),
        answer_text_sha256: 'sha256:test',
        attempt_id: id('27'),
        message_id: id('23'),
        run_id: id('24'),
        citation_ids: [citationId],
        claims: [{
          schema_version: 'accepted-answer-claim-presentation:v1',
          claim_id: claimId,
          kind: 'factual',
          start_offset: 0,
          end_offset: 19,
        }],
        relationships: [{
          schema_version: 'accepted-answer-relationship-presentation:v1',
          relationship_id: id('28'),
          claim_id: claimId,
          kind: 'independent',
          citation_ids: [citationId],
          related_claim_id: null,
        }],
      },
    };

    const mapped = mapApiMessage(message, {
      [citationId]: {
        source_object_id: id('30'),
        source_revision_id: id('31'),
        revision_number: 4,
        locator_reference: 'page:7',
        excerpt: 'Срок хранения — 36 месяцев.',
      },
    });

    expect(mapped.blocks[0].parts[0]).toEqual({ text: 'Срок — 36 месяцев.', claim: claimId });
    expect(mapped.claims[claimId].rel[0].cites).toEqual([0]);
    expect(mapped.citations[0]).toMatchObject({
      citation_id: citationId,
      source_object_id: id('30'),
      excerpt: 'Срок хранения — 36 месяцев.',
    });
    expect(mapped.run.duration_ms).toBe(2_000);
  });
});
