import { useEffect, useRef, useState } from 'react';

import { ru } from '../../i18n/resources';
import type { ChatEvent, Message, Session } from './protocol';
import type { ChatTransport } from './transport';
import styles from '../../app/AppShell.module.css';

export function ChatPanel({
  sessionId,
  transport,
  reconnectCount,
}: {
  sessionId: string;
  transport: ChatTransport;
  reconnectCount: number;
}) {
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [lastInput, setLastInput] = useState('');
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [state, setState] = useState<'loading' | 'ready' | 'running' | 'completed' | 'cancelled' | 'failed'>('loading');
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const commandNumberRef = useRef(0);

  useEffect(() => {
    let active = true;
    setState('loading');
    setError(null);
    void transport
      .getSession(sessionId)
      .then(async (nextSession) => {
        const nextMessages = await transport.listMessages(nextSession.id);
        if (!active) return;
        setSession(nextSession);
        setMessages(nextMessages);
        setState(nextMessages.length > 0 ? 'completed' : 'ready');
      })
      .catch(() => {
        if (!active) return;
        setState('failed');
        setError('Сессия недоступна для текущей области.');
      });
    return () => {
      active = false;
      controllerRef.current?.abort();
    };
  }, [sessionId, transport]);

  useEffect(() => {
    if (reconnectCount > 0 && state === 'running') return;
    if (reconnectCount === 0) setEvents([]);
  }, [reconnectCount, state]);

  async function submit(text = input) {
    const trimmed = text.trim();
    if (!session || !trimmed || controllerRef.current) return;

    commandNumberRef.current += 1;
    const idempotencyKey = `browser-command-${commandNumberRef.current}`;
    const controller = new AbortController();
    controllerRef.current = controller;
    setInput('');
    setLastInput(trimmed);
    setEvents([]);
    setError(null);
    setState('running');

    try {
      for await (const event of transport.sendMessage(
        session.id,
        { text: trimmed, idempotencyKey },
        controller.signal,
      )) {
        setEvents((previous) => [...previous, event]);
        if (event.type === 'cancelled') setState('cancelled');
        if (event.type === 'failed') setState('failed');
        if (event.type === 'completed' || event.type === 'abstained' || event.type === 'denied') {
          setState(event.type === 'completed' ? 'completed' : 'failed');
        }
      }
      const updatedMessages = await transport.listMessages(session.id);
      setMessages(updatedMessages);
    } catch {
      setState('failed');
      setError(ru.chat.failed);
    } finally {
      controllerRef.current = null;
    }
  }

  function cancel() {
    controllerRef.current?.abort();
  }

  return (
    <section className={styles.pageSection} aria-labelledby="chat-heading">
      <p className={styles.eyebrow}>{ru.page.eyebrow}</p>
      <h2 id="chat-heading">{ru.page.chatTitle}</h2>
      <p className={styles.lede}>{ru.page.chatDescription}</p>

      <div className={styles.chatMeta} role="status" aria-live="polite">
        <strong>{stateCopy(state)}</strong>
        {reconnectCount > 0 ? <span>{ru.chat.reconnecting} ({reconnectCount})</span> : null}
      </div>

      {state === 'loading' ? <p role="status">{ru.chat.loading}</p> : null}
      {error ? <p className={styles.chatError} role="alert">{error}</p> : null}
      {state !== 'loading' && messages.length === 0 ? <p>{ru.chat.empty}</p> : null}

      <div className={styles.chatMessages} aria-label="Канонические сообщения">
        {messages.map((message) => (
          <article className={styles.chatMessage} key={message.id}>
            <span className={styles.chatMessageLabel}>{ru.chat.canonical}</span>
            <p>{message.text}</p>
          </article>
        ))}
      </div>

      <div className={styles.chatEvents} aria-label="События SSE" aria-live="polite">
        {events.map((event) => (
          <span key={`${event.eventId}-${event.sequence}`}>{event.type}</span>
        ))}
      </div>

      <form
        className={styles.chatComposer}
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label htmlFor="chat-question">{ru.chat.inputLabel}</label>
        <textarea
          id="chat-question"
          onChange={(event) => setInput(event.target.value)}
          placeholder={ru.chat.inputPlaceholder}
          value={input}
        />
        <div className={styles.chatActions}>
          <button className={styles.primaryButton} disabled={!session || state === 'loading' || Boolean(controllerRef.current)} type="submit">
            {ru.chat.send}
          </button>
          {controllerRef.current ? (
            <button className={styles.secondaryButton} onClick={cancel} type="button">
              {ru.chat.cancel}
            </button>
          ) : null}
          {!controllerRef.current && (state === 'failed' || state === 'cancelled') && lastInput ? (
            <button className={styles.secondaryButton} onClick={() => void submit(lastInput)} type="button">
              {ru.chat.retry}
            </button>
          ) : null}
        </div>
      </form>

      <button className={styles.textButton} onClick={() => void transport.createSession()} type="button">
        {ru.chat.createSession}
      </button>
    </section>
  );
}

function stateCopy(state: 'loading' | 'ready' | 'running' | 'completed' | 'cancelled' | 'failed'): string {
  if (state === 'loading') return ru.chat.loading;
  if (state === 'ready') return ru.chat.ready;
  if (state === 'running') return ru.chat.running;
  if (state === 'completed') return ru.chat.completed;
  if (state === 'cancelled') return ru.chat.cancelled;
  return ru.chat.failed;
}
