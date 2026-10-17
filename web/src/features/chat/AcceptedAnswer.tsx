import { useEffect, useRef, useState } from 'react';

import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { loadQAMessage, type QAMessage } from './session-api';
import { inspectAcceptedCitation, type CitationInspection } from './citation-inspection';
import { buildCitationPlacements, type ClaimCitationPlacement } from './citation-presentation';
import { isSafeMarkdownSource, SafeMarkdown } from './SafeMarkdown';
import styles from './SessionBrowserPage.module.css';

type AnswerState = 'checking' | 'available' | 'unavailable' | 'unsafe';
export function AcceptedAnswer({
  client,
  message,
  scope,
}: {
  client: ScopedApiClient;
  message: QAMessage;
  scope: Scope;
}) {
  const [state, setState] = useState<AnswerState>('checking');
  const [placements, setPlacements] = useState<ClaimCitationPlacement[]>([]);
  const [inspection, setInspection] = useState<CitationInspection | null>(null);
  const [inspectionPending, setInspectionPending] = useState(false);
  const [inspectionAttempted, setInspectionAttempted] = useState(false);
  const request = useRef<AbortController | null>(null);
  const answer = message.answer;
  const presentation = message.accepted_presentation;

  useEffect(() => {
    request.current?.abort();
    setState('checking');
    setPlacements([]);
    setInspection(null);
    setInspectionPending(false);
    setInspectionAttempted(false);
    let active = true;
    void buildCitationPlacements(message).then((result) => {
      if (!active) return;
      if (!result) {
        setState('unavailable');
        return;
      }
      if (!answer || !isSafeMarkdownSource(answer)) {
        setState('unsafe');
        return;
      }
      setPlacements(result);
      setState('available');
    }).catch(() => {
      if (active) setState('unavailable');
    });
    return () => {
      active = false;
      request.current?.abort();
    };
  }, [answer, client, message, scope.environmentId, scope.workspaceId]);

  async function onInspect(citationId: string): Promise<void> {
    if (!presentation || !message.session_id || !message.run_id) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState('checking');
    setInspectionPending(true);
    setInspectionAttempted(true);
    setInspection(null);
    try {
      const currentMessage = await loadQAMessage(
        client,
        scope,
        message.session_id,
        message.message_id,
        controller.signal,
      );
      const currentPlacements = await buildCitationPlacements(currentMessage);
      if (
        !currentPlacements ||
        !currentMessage.answer ||
        !isSafeMarkdownSource(currentMessage.answer) ||
        !sameAcceptedPresentation(message, currentMessage) ||
        !hasCitation(currentPlacements, citationId)
      ) {
        throw new Error('The accepted answer is unavailable.');
      }

      const inspected = await inspectAcceptedCitation(
        client,
        scope,
        {
          answerId: presentation.answer_id,
          attemptId: presentation.attempt_id,
          runId: presentation.run_id,
        },
        citationId,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (inspected.status === 'unavailable') {
        setState('unavailable');
        return;
      }
      setPlacements(currentPlacements);
      setInspection(inspected);
      setState('available');
    } catch {
      if (!controller.signal.aborted) setState('unavailable');
    } finally {
      if (request.current === controller) {
        request.current = null;
        setInspectionPending(false);
      }
    }
  }

  if (state === 'checking') {
    return (
      <p className={styles.answerUnavailable} role="status" aria-live="polite">
        {inspectionPending ? ru.chat.citationChecking : ru.chat.answerChecking}
      </p>
    );
  }
  if (state !== 'available' || !answer || !presentation) {
    const message = state === 'unsafe'
      ? ru.chat.answerUnsafe
      : inspectionAttempted
        ? ru.chat.citationUnavailable
        : ru.chat.answerUnavailable;
    return <p className={styles.answerUnavailable} role="status">{message}</p>;
  }

  return (
    <div className={styles.acceptedAnswer}>
      <span className={styles.messageLabel}>{ru.chat.answerLabel}</span>
      <SafeMarkdown source={answer} />
      <ClaimCitations
        onInspect={onInspect}
        placements={placements}
        citationIds={presentation.citation_ids}
      />
      {inspection?.status === 'available' ? (
        <CitationEvidence
          citationNumber={citationNumber(presentation.citation_ids, inspection.evidence.citation_id)}
          evidence={inspection.evidence}
          onClose={() => setInspection(null)}
        />
      ) : null}
    </div>
  );
}

function ClaimCitations({
  onInspect,
  placements,
  citationIds,
}: {
  onInspect: (citationId: string) => void;
  placements: ClaimCitationPlacement[];
  citationIds: string[];
}) {
  const claimNumberById = new Map(placements.map((claim, index) => [claim.claimId, index + 1]));
  return (
    <section aria-label={ru.chat.claimCitationsTitle} className={styles.claimCitations}>
      <h4>{ru.chat.claimCitationsTitle}</h4>
      <ol>
        {placements.map((claim) => (
          <li key={claim.claimId}>
            <blockquote>
              <mark
                className={styles.acceptedClaim}
                data-claim-ids={claim.claimId}
                data-start-utf16={claim.utf16StartOffset}
                data-end-utf16={claim.utf16EndOffset}
              >
                {claim.text}
              </mark>
            </blockquote>
            <div className={styles.claimRelationshipList}>
              {claim.relationships.map((relationship) => {
                const relationLabel = ru.chat.relationshipKinds[relationship.kind];
                if (relationship.kind === 'contradiction') {
                  const relatedClaimNumber = relationship.related_claim_id
                    ? claimNumberById.get(relationship.related_claim_id)
                    : undefined;
                  return (
                    <span className={styles.claimRelationship} key={relationship.relationship_id}>
                      {ru.chat.relatedClaimRelationship(relationLabel, relatedClaimNumber ?? 0)}
                    </span>
                  );
                }
                return relationship.citation_ids.map((citationId) => {
                  const number = citationNumber(citationIds, citationId);
                  if (number === 0) return null;
                  return (
                    <span className={styles.claimRelationship} key={`${relationship.relationship_id}-${citationId}`}>
                      {relationLabel}{' '}
                      <sup>
                        <button
                          aria-label={ru.chat.citationMarkerLabel(number, relationLabel)}
                          className={styles.citationMarker}
                          onClick={() => onInspect(citationId)}
                          type="button"
                        >
                          {number}
                        </button>
                      </sup>
                    </span>
                  );
                });
              })}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function CitationEvidence({
  citationNumber: number,
  evidence,
  onClose,
}: {
  citationNumber: number;
  evidence: Extract<CitationInspection, { status: 'available' }>['evidence'];
  onClose: () => void;
}) {
  return (
    <section aria-labelledby="citation-evidence-heading" className={styles.citationEvidence}>
      <header>
        <h4 id="citation-evidence-heading">{ru.chat.citationEvidenceTitle(number)}</h4>
        <button className={styles.textButton} onClick={onClose} type="button">
          {ru.chat.closeCitationEvidence}
        </button>
      </header>
      <p className={styles.citationExcerpt}>{evidence.excerpt}</p>
      <dl>
        <div>
          <dt>{ru.chat.citationSourceRevision}</dt>
          <dd>{evidence.source_revision_id}{evidence.revision_number ? ` · ${evidence.revision_number}` : ''}</dd>
        </div>
        <div>
          <dt>{ru.chat.citationSourceLocation}</dt>
          <dd>{evidence.locator_schema}: {evidence.locator_reference}</dd>
        </div>
      </dl>
    </section>
  );
}

function citationNumber(citationIds: string[], citationId: string): number {
  const index = citationIds.indexOf(citationId);
  return index < 0 ? 0 : index + 1;
}

function hasCitation(placements: ClaimCitationPlacement[], citationId: string): boolean {
  return placements.some((claim) =>
    claim.relationships.some((relationship) => relationship.citation_ids.includes(citationId)),
  );
}

function sameAcceptedPresentation(original: QAMessage, current: QAMessage): boolean {
  const first = original.accepted_presentation;
  const second = current.accepted_presentation;
  if (
    !first ||
    !second ||
    original.message_id !== current.message_id ||
    original.session_id !== current.session_id ||
    original.answer !== current.answer ||
    original.run_id !== current.run_id ||
    current.disclosure_eligibility !== 'eligible' ||
    current.state !== 'terminal' ||
    current.terminal_outcome !== 'answered'
  ) {
    return false;
  }
  return acceptedPresentationSignature(first) === acceptedPresentationSignature(second);
}

function acceptedPresentationSignature(presentation: NonNullable<QAMessage['accepted_presentation']>): string {
  return JSON.stringify([
    presentation.schema_version,
    presentation.message_id,
    presentation.run_id,
    presentation.attempt_id,
    presentation.answer_id,
    presentation.answer_text_sha256,
    [...presentation.citation_ids].sort(),
    presentation.claims
      .map(({ claim_id, kind, start_offset, end_offset }) => [claim_id, kind, start_offset, end_offset])
      .sort(([leftId], [rightId]) => String(leftId).localeCompare(String(rightId))),
    presentation.relationships
      .map(({ relationship_id, claim_id, kind, related_claim_id, citation_ids }) => [
        relationship_id,
        claim_id,
        kind,
        related_claim_id,
        [...citation_ids].sort(),
      ])
      .sort(([leftId], [rightId]) => String(leftId).localeCompare(String(rightId))),
  ]);
}
