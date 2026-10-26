import { useEffect, useRef, useState } from "react";
import { ArrowUp, CornerDownRight, Square, X } from "lucide-react";
import { Spinner, Tooltip, interactiveBase, motion, tokens, usePal } from "../../halaska-kit";
import { idTail } from "../spineData";
import { ru } from "../ru";
import { mono } from "./chatStyles";

const C = ru.chat;

export default function SpineChatComposer({ chat, inputRef }) {
  const pal = usePal();
  const [focus, setFocus] = useState(false);
  const localRef = useRef(null);
  const ref = inputRef ?? localRef;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [chat.draft, ref]);

  useEffect(() => { if (chat.clarifyTarget) ref.current?.focus(); }, [chat.clarifyTarget, ref]);

  const locked = chat.submitting || !!chat.running;
  const canSend = chat.draft.trim().length > 0 && !locked;
  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); chat.ask(); }
    if (e.key === "Backspace" && chat.clarifyTarget && e.currentTarget.selectionStart === 0 && e.currentTarget.selectionEnd === 0) chat.setClarifyTarget(null);
  };

  return (
    <form onSubmit={(e) => { e.preventDefault(); chat.ask(); }} aria-label={C.inputLabel} style={{
      borderRadius: tokens.radius.md, background: pal.bgElevated ?? pal.bg, border: `1px solid ${focus ? pal.border : pal.borderSubtle}`,
      boxShadow: `0 8px 32px ${pal.shadow}`, transition: `border-color ${motion.normal} ${motion.easeInOut}`,
    }}>
      <div onClick={() => ref.current?.focus()} style={{
        margin: 6, display: "flex", alignItems: "flex-end", gap: 8, padding: "7px 7px 7px 12px", borderRadius: tokens.radius.sm, background: pal.bgSubtle, cursor: "text",
      }}>
        {chat.clarifyTarget && (
          <Tooltip text={`${C.clarificationReply} …${idTail(chat.clarifyTarget)} · Backspace — ${C.cancelClarification.toLowerCase()}`}>
            <span role="status" style={{
              ...mono, display: "inline-flex", alignItems: "center", gap: 4, height: 22, padding: "0 2px 0 6px", flexShrink: 0, marginBottom: 4,
              borderRadius: 5, border: `1px solid ${pal.border}`, color: pal.textSecondary, background: pal.bg,
            }}>
              <CornerDownRight size={11} strokeWidth={2} />Уточнение
              <button type="button" aria-label={C.cancelClarification} onClick={(e) => { e.stopPropagation(); chat.setClarifyTarget(null); }} style={{
                ...interactiveBase, width: 16, height: 16, display: "inline-flex", alignItems: "center", justifyContent: "center", borderRadius: 3, background: "transparent", color: pal.textTertiary,
              }}><X size={10} strokeWidth={2} /></button>
            </span>
          </Tooltip>
        )}
        <textarea ref={ref} rows={1} value={chat.draft} onChange={(e) => chat.setDraft(e.target.value)} onKeyDown={onKeyDown}
          onFocus={() => setFocus(true)} onBlur={() => setFocus(false)} aria-label={C.inputLabel} disabled={chat.submitting}
          placeholder={chat.clarifyTarget ? "Ответьте на уточнение" : `${C.inputPlaceholder}…`}
          style={{
            flex: 1, minWidth: 0, resize: "none", border: "none", outline: "none", background: "transparent", padding: "4px 0",
            ...tokens.type.base, lineHeight: 1.5, color: pal.text, fontFamily: tokens.font.sans,
          }} />
        {chat.running ? (
          <Tooltip text={`${C.cancelRequest} · Esc`}>
            <button type="button" aria-label={C.cancelRequest} onClick={chat.stop} disabled={!!chat.running.cancel} style={{
              ...interactiveBase, width: 30, height: 30, borderRadius: 15, display: "inline-flex", alignItems: "center", justifyContent: "center",
              background: pal.text, color: pal.textInverse, opacity: chat.running.cancel ? 0.5 : 1,
            }}>{chat.running.cancel ? <Spinner size={12} color={pal.textInverse} /> : <Square size={11} strokeWidth={0} fill="currentColor" />}</button>
          </Tooltip>
        ) : (
          <button type="submit" aria-label={chat.submitting ? C.askSubmitting : C.send} disabled={!canSend} style={{
            ...interactiveBase, width: 30, height: 30, borderRadius: 15, display: "inline-flex", alignItems: "center", justifyContent: "center",
            background: canSend || chat.submitting ? pal.text : pal.bgMuted, color: canSend || chat.submitting ? pal.textInverse : pal.textMuted,
            cursor: canSend ? "pointer" : "default", transition: `background ${motion.normal} ${motion.easeInOut}`,
          }}>{chat.submitting ? <Spinner size={12} color={pal.textInverse} /> : <ArrowUp size={16} strokeWidth={2} />}</button>
        )}
      </div>
    </form>
  );
}
