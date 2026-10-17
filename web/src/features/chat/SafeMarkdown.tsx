import type { ReactNode } from 'react';

import { ru } from '../../i18n/resources';
import styles from './SessionBrowserPage.module.css';

type Block =
  | { kind: 'paragraph'; lines: string[] }
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'code'; text: string };

const rawHtmlPattern = /<!--|<!doctype\b|<\/?[a-z][^>]*>/i;
const inlineCodePattern = /`[^`\n]*`/g;

export function SafeMarkdown({ source }: { source: string }) {
  const blocks = parseBlocks(source);
  if (!blocks) {
    return <p className={styles.answerUnavailable} role="status">{ru.chat.answerUnsafe}</p>;
  }

  return (
    <div className={styles.answerMarkdown}>
      {blocks.map((block, index) => {
        if (block.kind === 'heading') {
          return renderHeading(block.level, renderInline(block.text), index);
        }
        if (block.kind === 'list') {
          const List = block.ordered ? 'ol' : 'ul';
          return (
            <List key={index}>
              {block.items.map((item, itemIndex) => <li key={itemIndex}>{renderInline(item)}</li>)}
            </List>
          );
        }
        if (block.kind === 'code') {
          return <pre key={index}><code>{block.text}</code></pre>;
        }
        return (
          <p key={index}>
            {block.lines.map((line, lineIndex) => (
              <span key={lineIndex}>
                {lineIndex > 0 ? <br /> : null}
                {renderInline(line)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function parseBlocks(source: string): Block[] | null {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let code: string[] | null = null;
  let hasUnsafeHtml = false;

  const flushParagraph = () => {
    if (paragraph.length > 0) blocks.push({ kind: 'paragraph', lines: paragraph });
    paragraph = [];
  };
  const flushList = () => {
    if (list) blocks.push({ kind: 'list', ...list });
    list = null;
  };

  for (const line of lines) {
    if (code) {
      if (/^\s*```\s*$/.test(line)) {
        blocks.push({ kind: 'code', text: code.join('\n') });
        code = null;
      } else {
        code.push(line);
      }
      continue;
    }
    if (/^\s*```/.test(line)) {
      flushParagraph();
      flushList();
      code = [];
      continue;
    }
    if (hasRawHtmlOutsideInlineCode(line)) hasUnsafeHtml = true;

    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ kind: 'heading', level: heading[1]!.length, text: heading[2]! });
      continue;
    }

    const listItem = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/.exec(line);
    if (listItem) {
      flushParagraph();
      const ordered = Boolean(listItem[2]);
      if (list && list.ordered !== ordered) flushList();
      list ??= { ordered, items: [] };
      list.items.push(listItem[3]!);
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  flushParagraph();
  flushList();
  if (code) blocks.push({ kind: 'code', text: code.join('\n') });
  return hasUnsafeHtml ? null : blocks;
}

function hasRawHtmlOutsideInlineCode(line: string): boolean {
  return line.replace(inlineCodePattern, '').match(rawHtmlPattern) !== null;
}

function renderInline(source: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const tokenPattern = /(`[^`\n]*`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|!?\[[^\]]*\]\([^)]*\))/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenPattern.exec(source)) !== null) {
    if (match.index > lastIndex) nodes.push(source.slice(lastIndex, match.index));
    const token = match[0];
    if (token.startsWith('`')) {
      nodes.push(<code key={match.index}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**') || token.startsWith('__')) {
      nodes.push(<strong key={match.index}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith('*') || token.startsWith('_')) {
      nodes.push(<em key={match.index}>{token.slice(1, -1)}</em>);
    } else {
      nodes.push(renderLinkOrText(token, match.index));
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < source.length) nodes.push(source.slice(lastIndex));
  return nodes;
}

function renderLinkOrText(token: string, key: number): ReactNode {
  const match = /^(!?)\[([^\]]*)\]\(([^)]*)\)$/.exec(token);
  if (!match || match[1] === '!') return match?.[2] ?? token;
  const href = safeHref(match[3]!);
  if (!href) return match[2];
  return <a key={key} href={href} rel="noopener noreferrer" target="_blank">{match[2]}</a>;
}

function safeHref(value: string): string | null {
  try {
    const url = new URL(value.trim());
    if ((url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password) {
      return url.href;
    }
  } catch {
    return null;
  }
  return null;
}

function renderHeading(level: number, children: ReactNode[], key: number): ReactNode {
  switch (level) {
    case 1: return <h4 key={key}>{children}</h4>;
    case 2: return <h5 key={key}>{children}</h5>;
    default: return <h6 key={key}>{children}</h6>;
  }
}
