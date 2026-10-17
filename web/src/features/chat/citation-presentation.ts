import type { components } from '../../../api/generated/schema';
import type { QAMessage } from './session-api';
import { codePointSpanToUtf16 } from './protocol';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RELATIONSHIP_KINDS = new Set<components['schemas']['ClaimEvidenceRelationshipKind']>([
  'independent',
  'corroborating',
  'joint',
  'derivation_input',
  'contradiction',
]);
type PresentedClaim = components['schemas']['AcceptedAnswerClaimPresentation'];

export type ClaimCitationRelationship = components['schemas']['AcceptedAnswerRelationshipPresentation'];

export interface ClaimCitationPlacement {
  claimId: string;
  endOffset: number;
  kind: components['schemas']['AnswerClaimKind'];
  relationships: ClaimCitationRelationship[];
  startOffset: number;
  text: string;
  utf16EndOffset: number;
  utf16StartOffset: number;
}

export async function buildCitationPlacements(
  message: QAMessage,
): Promise<ClaimCitationPlacement[] | null> {
  const answer: unknown = message.answer;
  const presentation = message.accepted_presentation;
  if (
    typeof answer !== 'string' ||
    !answer ||
    !answer.trim() ||
    message.state !== 'terminal' ||
    message.terminal_outcome !== 'answered' ||
    message.disclosure_eligibility !== 'eligible' ||
    !message.run_id ||
    !presentation ||
    presentation.schema_version !== 'accepted-answer-presentation:v1' ||
    presentation.message_id !== message.message_id ||
    presentation.run_id !== message.run_id ||
    !UUID_PATTERN.test(presentation.answer_id) ||
    !UUID_PATTERN.test(presentation.attempt_id) ||
    !/^[0-9a-f]{64}$/.test(presentation.answer_text_sha256) ||
    !(await answerDigestMatches(answer, presentation.answer_text_sha256))
  ) {
    return null;
  }

  const citationIds: unknown = presentation.citation_ids;
  const claims: unknown = presentation.claims;
  const relationships: unknown = presentation.relationships;
  if (
    !Array.isArray(citationIds) ||
    !Array.isArray(claims) ||
    !Array.isArray(relationships) ||
    citationIds.length === 0 ||
    claims.length === 0 ||
    relationships.length === 0 ||
    !hasUniqueValidIds(citationIds)
  ) {
    return null;
  }

  const claimIds = new Set<string>();
  const codePointLength = Array.from(answer).length;
  const placements: ClaimCitationPlacement[] = [];
  for (const value of claims as unknown[]) {
    if (!isPresentedClaim(value)) return null;
    const claim = value;
    if (
      claim.schema_version !== 'accepted-answer-claim-presentation:v1' ||
      !UUID_PATTERN.test(claim.claim_id) ||
      claimIds.has(claim.claim_id) ||
      (claim.kind !== 'factual' && claim.kind !== 'derived') ||
      !Number.isSafeInteger(claim.start_offset) ||
      !Number.isSafeInteger(claim.end_offset) ||
      claim.start_offset < 0 ||
      claim.end_offset <= claim.start_offset ||
      claim.end_offset > codePointLength
    ) {
      return null;
    }
    claimIds.add(claim.claim_id);
    let offsets: ReturnType<typeof codePointSpanToUtf16>;
    try {
      offsets = codePointSpanToUtf16(answer, claim.start_offset, claim.end_offset);
    } catch {
      return null;
    }
    placements.push({
      claimId: claim.claim_id,
      endOffset: claim.end_offset,
      kind: claim.kind,
      relationships: [],
      startOffset: claim.start_offset,
      text: answer.slice(offsets.startOffset, offsets.endOffset),
      utf16EndOffset: offsets.endOffset,
      utf16StartOffset: offsets.startOffset,
    });
  }

  const relationshipIds = new Set<string>();
  const citedIds = new Set<string>();
  const citationRolesByClaim = new Map<string, Set<string>>();
  for (const value of relationships as unknown[]) {
    if (!isPresentedRelationship(value)) return null;
    const relationship = value;
    const relatedClaimId = relationship.related_claim_id ?? null;
    if (
      relationship.schema_version !== 'accepted-answer-relationship-presentation:v1' ||
      !UUID_PATTERN.test(relationship.relationship_id) ||
      relationshipIds.has(relationship.relationship_id) ||
      !claimIds.has(relationship.claim_id) ||
      !RELATIONSHIP_KINDS.has(relationship.kind) ||
      !hasUniqueValidIds(relationship.citation_ids) ||
      relationship.citation_ids.some((id) => !citationIds.includes(id)) ||
      (relatedClaimId !== null && !claimIds.has(relatedClaimId))
    ) {
      return null;
    }
    if (relationship.kind === 'contradiction') {
      if (
        !relatedClaimId ||
        relatedClaimId === relationship.claim_id ||
        relationship.citation_ids.length > 0
      ) {
        return null;
      }
    } else if (
      relatedClaimId !== null ||
      ((relationship.kind === 'independent' || relationship.kind === 'corroborating') &&
        relationship.citation_ids.length !== 1) ||
      (relationship.kind === 'joint' && relationship.citation_ids.length < 2) ||
      (relationship.kind === 'derivation_input' && relationship.citation_ids.length === 0)
    ) {
      return null;
    }

    const roles = citationRolesByClaim.get(relationship.claim_id) ?? new Set<string>();
    if (relationship.citation_ids.some((id) => roles.has(id))) return null;
    relationship.citation_ids.forEach((id) => {
      roles.add(id);
      citedIds.add(id);
    });
    citationRolesByClaim.set(relationship.claim_id, roles);
    relationshipIds.add(relationship.relationship_id);
    const placement = placements.find((item) => item.claimId === relationship.claim_id);
    if (!placement) return null;
    placement.relationships.push(relationship);
  }

  if (citedIds.size !== citationIds.length || citationIds.some((id) => !citedIds.has(id))) {
    return null;
  }
  for (const placement of placements) {
    const evidenceKinds = new Set(
      placement.relationships
        .filter((item) => item.kind !== 'contradiction')
        .map((item) => item.kind),
    );
    if (
      (placement.kind === 'derived' &&
        (evidenceKinds.size !== 1 || !evidenceKinds.has('derivation_input'))) ||
      (placement.kind === 'factual' &&
        (evidenceKinds.has('derivation_input') ||
          (!evidenceKinds.has('independent') && !evidenceKinds.has('joint'))))
    ) {
      return null;
    }
  }
  return placements.sort((left, right) => left.startOffset - right.startOffset || left.endOffset - right.endOffset);
}

async function answerDigestMatches(answer: string, expected: string): Promise<boolean> {
  try {
    if (!globalThis.crypto?.subtle) return false;
    const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(answer));
    const actual = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
    return actual === expected;
  } catch {
    return false;
  }
}

function hasUniqueValidIds(ids: unknown): ids is string[] {
  return Array.isArray(ids) &&
    ids.every((id) => typeof id === 'string' && UUID_PATTERN.test(id)) &&
    new Set(ids).size === ids.length;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isPresentedClaim(value: unknown): value is PresentedClaim {
  if (!isRecord(value)) return false;
  return (
    typeof value.claim_id === 'string' &&
    typeof value.kind === 'string' &&
    Number.isSafeInteger(value.start_offset) &&
    Number.isSafeInteger(value.end_offset)
  );
}

function isPresentedRelationship(
  value: unknown,
): value is ClaimCitationRelationship {
  if (!isRecord(value)) return false;
  return (
    typeof value.relationship_id === 'string' &&
    typeof value.claim_id === 'string' &&
    typeof value.kind === 'string' &&
    Array.isArray(value.citation_ids) &&
    (value.related_claim_id === undefined || value.related_claim_id === null ||
      typeof value.related_claim_id === 'string')
  );
}
