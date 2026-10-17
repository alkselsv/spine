import { useEffect, useState } from 'react';

import type { QAMessage } from './session-api';
import { SafeMarkdown } from './SafeMarkdown';
import { ru } from '../../i18n/resources';
import styles from './SessionBrowserPage.module.css';

type AnswerValidation = 'checking' | 'accepted' | 'unavailable';

export function AcceptedAnswer({ message }: { message: QAMessage }) {
  const answer = message.answer;
  const presentation = message.accepted_presentation;
  const [validation, setValidation] = useState<AnswerValidation>('checking');

  useEffect(() => {
    let active = true;
    setValidation('checking');
    if (!answer || !presentation || !hasAcceptedIdentity(message)) {
      setValidation('unavailable');
      return () => { active = false; };
    }

    void hashMatches(answer, presentation.answer_text_sha256).then((matches) => {
      if (active) setValidation(matches ? 'accepted' : 'unavailable');
    });
    return () => { active = false; };
  }, [answer, message, presentation]);

  if (validation === 'checking') {
    return <p className={styles.answerUnavailable} role="status">{ru.chat.answerChecking}</p>;
  }
  if (validation !== 'accepted' || !answer) {
    return <p className={styles.answerUnavailable} role="status">{ru.chat.answerUnavailable}</p>;
  }

  return (
    <div>
      <span className={styles.messageLabel}>{ru.chat.answerLabel}</span>
      <SafeMarkdown source={answer} />
    </div>
  );
}

function hasAcceptedIdentity(message: QAMessage): boolean {
  const presentation = message.accepted_presentation;
  return Boolean(
    message.state === 'terminal' &&
    message.terminal_outcome === 'answered' &&
    message.disclosure_eligibility === 'eligible' &&
    message.run_id &&
    typeof message.answer === 'string' &&
    message.answer.trim() &&
    presentation &&
    presentation.schema_version === 'accepted-answer-presentation:v1' &&
    presentation.message_id === message.message_id &&
    presentation.run_id === message.run_id &&
    /^[0-9a-f]{64}$/.test(presentation.answer_text_sha256) &&
    Array.isArray(presentation.claims) &&
    presentation.claims.length > 0 &&
    Array.isArray(presentation.relationships) &&
    presentation.relationships.length > 0 &&
    Array.isArray(presentation.citation_ids) &&
    presentation.citation_ids.length > 0,
  );
}

async function hashMatches(answer: string, expected: string): Promise<boolean> {
  try {
    if (!globalThis.crypto?.subtle) return false;
    const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(answer));
    const actual = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
    return actual === expected;
  } catch {
    return false;
  }
}
