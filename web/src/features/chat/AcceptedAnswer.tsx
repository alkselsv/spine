import { useEffect, useRef, useState } from 'react';

import type { ScopedApiClient } from '../../api/client';
import type { Scope } from '../../app/scope';
import { ru } from '../../i18n/resources';
import { loadQAMessage, type QAMessage } from './session-api';
import { inspectAcceptedCitation, type CitationInspection } from './citation-inspection';
import { buildCitationPlacements, type ClaimCitationPlacement } from './citation-presentation';
import { isSafeMarkdownSource, SafeMarkdown, type InlineMarkdownMarker } from './SafeMarkdown';
import { FeedbackControls } from './FeedbackControls';
import styles from './SessionBrowserPage.module.css';

type AnswerState = 'checking' | 'available' | 'unavailable' | 'unsafe';
export function AcceptedAnswer({
  client,
  message,
  targetCitationId = null,
  scope,
}: {
  client: ScopedApiClient;
  message: QAMessage;
  targetCitationId?: string | null;
  scope: Scope;
}) {
  const [state, setState] = useState<AnswerState>('checking');
  const [placements, setPlacements] = useState<ClaimCitationPlacement[]>([]);
  const [inspection, setInspection] = useState<CitationInspection | null>(null);
  const [inspectionPending, setInspectionPending] = useState(false);
  const [inspectionAttempted, setInspectionAttempted] = useState(false);
  const [validatedMessage, setValidatedMessage] = useState<QAMessage | null>(null);
  const request = useRef<AbortController | null>(null);
  const autoInspection = useRef<string | null>(null);
  const answer = message.answer;
  const presentation = message.accepted_presentation;
  const inlineMarkers = placements.flatMap((claim): InlineMarkdownMarker[] =>
    claim.relationships.flatMap((relationship) => {
      const relationLabel = ru.chat.relationshipKinds[relationship.kind];
      const common = {
        claimId: claim.claimId,
        endOffset: claim.utf16EndOffset,
        key: `${relationship.relationship_id}:${claim.claimId}`,
        offset: claim.utf16EndOffset,
        startOffset: claim.utf16StartOffset,
      };
      if (relationship.kind === 'contradiction') {
        const claimNumber = placements.findIndex((item) => item.claimId === relationship.related_claim_id) + 1;
        return [{
          ...common,
          render: () => <span className={styles.claimRelationship}>
            {ru.chat.relatedClaimRelationship(relationLabel, claimNumber)}
          </span>,
        }];
      }
      return relationship.citation_ids.map((citationId): InlineMarkdownMarker => {
        const number = citationNumber(presentation?.citation_ids ?? [], citationId);
        return {
          ...common,
          key: `${common.key}:${citationId}`,
          render: () => (
            <span className={styles.claimRelationship}>
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
          ),
        };
      });
    }),
  );

  useEffect(() => {
    request.current?.abort();
    setValidatedMessage(null);
    setState('checking');
    setPlacements([]);
    setInspection(null);
    setInspectionPending(false);
    setInspectionAttempted(false);
    let active = true;
    void buildCitationPlacements(message).then((result) => {
      if (!active) return;
      if (!result) {
        setValidatedMessage(message);
        setState('unavailable');
        return;
      }
      if (!answer || !isSafeMarkdownSource(answer)) {
        setValidatedMessage(message);
        setState('unsafe');
        return;
      }
      setPlacements(result);
      setValidatedMessage(message);
      setState('available');
    }).catch(() => {
      if (active) {
        setValidatedMessage(message);
        setState('unavailable');
      }
    });
    return () => {
      active = false;
      request.current?.abort();
    };
  }, [answer, client, message, scope.environmentId, scope.workspaceId]);

  useEffect(() => {
    if (!targetCitationId) return;
    const requestKey = `${message.message_id}:${targetCitationId}`;
    if (autoInspection.current === requestKey) return;
    autoInspection.current = requestKey;
    if (!presentation?.citation_ids.includes(targetCitationId)) {
      setInspectionAttempted(true);
      setState('unavailable');
      return;
    }
    void onInspect(targetCitationId);
  }, [message.message_id, presentation, targetCitationId]);

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
      setValidatedMessage(message);
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

  if (state === 'checking' || validatedMessage !== message) {
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
      <SafeMarkdown markers={inlineMarkers} source={answer} />
      <FeedbackControls
        client={client}
        targetKind="answer"
        targetId={presentation.answer_id}
        runId={presentation.run_id}
        messageId={message.message_id}
      />
      {inspection?.status === 'available' ? (
        <CitationEvidence
          citationNumber={citationNumber(presentation.citation_ids, inspection.evidence.citation_id)}
          client={client}
          evidence={inspection.evidence}
          messageId={message.message_id}
          runId={presentation.run_id}
          onClose={() => setInspection(null)}
        />
      ) : null}
    </div>
  );
}

function CitationEvidence({
  citationNumber: number,
  client,
  evidence,
  messageId,
  runId,
  onClose,
}: {
  citationNumber: number;
  client: ScopedApiClient;
  evidence: Extract<CitationInspection, { status: 'available' }>['evidence'];
  messageId: string;
  runId: string;
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
      <FeedbackControls
        client={client}
        targetKind="citation"
        targetId={evidence.citation_id}
        runId={runId}
        messageId={messageId}
      />
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
