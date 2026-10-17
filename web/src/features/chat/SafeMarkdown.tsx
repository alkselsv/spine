import type { ReactNode } from 'react';

import { ru } from '../../i18n/resources';
import styles from './SessionBrowserPage.module.css';

type SourceLine = { startOffset: number; text: string };
export type InlineMarkdownMarker = {
  claimId: string;
  endOffset: number;
  key: string;
  offset: number;
  render: () => ReactNode;
  startOffset: number;
};
type Block =
  | { kind: 'paragraph'; lines: SourceLine[] }
  | { kind: 'heading'; level: number; line: SourceLine }
  | { kind: 'list'; ordered: boolean; items: SourceLine[] }
  | { kind: 'code'; lines: SourceLine[] };

export function isSafeMarkdownSource(source: string): boolean {
  return parseBlocks(source) !== null;
}

const rawHtmlPattern = /<!--|<!doctype\b|<\/?[a-z][^>]*>/i;
const inlineCodePattern = /`[^`\n]*`/g;

export function SafeMarkdown({ source, markers = [] }: { source: string; markers?: InlineMarkdownMarker[] }) {
  const blocks = parseBlocks(source);
  if (!blocks) {
    return <p className={styles.answerUnavailable} role="status">{ru.chat.answerUnsafe}</p>;
  }

  return (
    <div className={styles.answerMarkdown} data-accepted-answer-text>
      {blocks.map((block, index) => {
        if (block.kind === 'heading') {
          return renderHeading(block.level, renderInline(block.line.text, block.line.startOffset, markers), index);
        }
        if (block.kind === 'list') {
          const List = block.ordered ? 'ol' : 'ul';
          return (
            <List key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{renderInline(item.text, item.startOffset, markers)}</li>
              ))}
            </List>
          );
        }
        if (block.kind === 'code') {
          return (
            <pre key={index}><code>{block.lines.map((line, lineIndex) => (
              <span key={lineIndex}>
                {lineIndex > 0 ? '\n' : null}
                {renderText(line.text, line.startOffset, markers)}
              </span>
            ))}</code></pre>
          );
        }
        return (
          <p key={index}>
            {block.lines.map((line, lineIndex) => (
              <span key={lineIndex}>
                {lineIndex > 0 ? <br /> : null}
                {renderInline(line.text, line.startOffset, markers)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function parseBlocks(source: string): Block[] | null {
  let nextStartOffset = 0;
  const lines = source.split('\n').map((raw): SourceLine => {
    const startOffset = nextStartOffset;
    nextStartOffset += raw.length + 1;
    return { startOffset, text: raw.endsWith('\r') ? raw.slice(0, -1) : raw };
  });
  const blocks: Block[] = [];
  let paragraph: SourceLine[] = [];
  let list: { ordered: boolean; items: SourceLine[] } | null = null;
  let code: SourceLine[] | null = null;
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
      if (/^\s*```\s*$/.test(line.text)) {
        blocks.push({ kind: 'code', lines: code });
        code = null;
      } else {
        code.push(line);
      }
      continue;
    }
    if (/^\s*```/.test(line.text)) {
      flushParagraph();
      flushList();
      code = [];
      continue;
    }
    if (hasRawHtmlOutsideInlineCode(line.text)) hasUnsafeHtml = true;

    if (!line.text.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = /^(#{1,6})\s+(.+)$/.exec(line.text);
    if (heading) {
      flushParagraph();
      flushList();
      const prefix = /^(?:#{1,6})\s+/.exec(line.text)?.[0] ?? '';
      blocks.push({
        kind: 'heading',
        level: heading[1]!.length,
        line: { text: heading[2]!, startOffset: line.startOffset + prefix.length },
      });
      continue;
    }

    const listItem = /^(\s*(?:([-*+])|(\d+)\.)\s+)(.+)$/.exec(line.text);
    if (listItem) {
      flushParagraph();
      const ordered = Boolean(listItem[3]);
      if (list && list.ordered !== ordered) flushList();
      list ??= { ordered, items: [] };
      list.items.push({ text: listItem[4]!, startOffset: line.startOffset + listItem[1]!.length });
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  flushParagraph();
  flushList();
  if (code) blocks.push({ kind: 'code', lines: code });
  return hasUnsafeHtml ? null : blocks;
}

function hasRawHtmlOutsideInlineCode(line: string): boolean {
  return line.replace(inlineCodePattern, '').match(rawHtmlPattern) !== null;
}

function renderInline(source: string, startOffset: number, markers: InlineMarkdownMarker[]): ReactNode[] {
  const nodes: ReactNode[] = [];
  const tokenPattern = /(`[^`\n]*`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|!?\[[^\]]*\]\([^)]*\))/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenPattern.exec(source)) !== null) {
    if (match.index > lastIndex) nodes.push(...renderText(source.slice(lastIndex, match.index), startOffset + lastIndex, markers));
    const token = match[0];
    if (token.startsWith('`')) {
      nodes.push(<code key={match.index}>{renderInline(token.slice(1, -1), startOffset + match.index + 1, markers)}</code>);
    } else if (token.startsWith('**') || token.startsWith('__')) {
      nodes.push(<strong key={match.index}>{renderInline(token.slice(2, -2), startOffset + match.index + 2, markers)}</strong>);
    } else if (token.startsWith('*') || token.startsWith('_')) {
      nodes.push(<em key={match.index}>{renderInline(token.slice(1, -1), startOffset + match.index + 1, markers)}</em>);
    } else {
      const link = /^(!?)\[([^\]]*)\]\(([^)]*)\)$/.exec(token);
      if (link && link[1] !== '!') {
        const labelStart = startOffset + match.index + link[1]!.length + 1;
        const labelEnd = labelStart + link[2]!.length;
        const containedMarkers = markers.filter((marker) => marker.offset >= labelStart && marker.offset < labelEnd);
        const before = markers.filter((marker) => marker.offset >= startOffset + match!.index && marker.offset < labelStart);
        const after = markers.filter((marker) => marker.offset >= labelEnd && marker.offset <= startOffset + match!.index + token.length);
        if (before.length) nodes.push(...before.map(renderMarker));
        const href = safeHref(link[3]!);
        if (href) {
          nodes.push(<a key={match.index} href={href} rel="noopener noreferrer" target="_blank">{renderInline(link[2]!, labelStart, containedMarkers)}</a>);
        } else {
          nodes.push(...renderInline(link[2]!, labelStart, containedMarkers));
        }
        if (after.length) nodes.push(...after.map(renderMarker));
      } else {
        nodes.push(renderLinkOrText(token, match.index));
      }
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < source.length) {
    nodes.push(...renderText(source.slice(lastIndex), startOffset + lastIndex, markers));
  } else {
    nodes.push(...renderText('', startOffset + source.length, markers));
  }
  return nodes;
}

function renderText(source: string, startOffset: number, markers: InlineMarkdownMarker[]): ReactNode[] {
  const matching = markers
    .filter((marker) => marker.offset >= startOffset && marker.offset <= startOffset + source.length)
    .sort((left, right) => left.offset - right.offset);
  const nodes: ReactNode[] = [];
  let lastOffset = 0;
  for (const marker of matching) {
    const markerOffset = marker.offset - startOffset;
    if (markerOffset < lastOffset) continue;
    if (markerOffset > lastOffset) nodes.push(source.slice(lastOffset, markerOffset));
    nodes.push(renderMarker(marker));
    lastOffset = markerOffset;
  }
  if (lastOffset < source.length) nodes.push(source.slice(lastOffset));
  return nodes;
}

function renderMarker(marker: InlineMarkdownMarker): ReactNode {
  return (
    <span
      data-claim-id={marker.claimId}
      data-start-utf16={marker.startOffset}
      data-end-utf16={marker.endOffset}
      key={marker.key}
    >
      {marker.render()}
    </span>
  );
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
