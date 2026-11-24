// Minimal, XSS-safe Markdown renderer for assistant answers: **bold**, *italic*, `code`, "- " lists,
// "1. " lists, paragraphs, and [n] citation markers. Builds React nodes — never uses innerHTML.

import { Fragment, type ReactNode } from "react";

const INLINE = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[\d+\])/g;

export function renderInline(text: string, keyPrefix = ""): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    const key = `${keyPrefix}${i}`;
    if (!part) return null;
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={key}>{part.slice(2, -2)}</strong>;
    if (/^\[\d+\]$/.test(part)) return <sup key={key} className="cite">{part}</sup>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={key}>{part.slice(1, -1)}</code>;
    if (part.startsWith("*") && part.endsWith("*")) return <em key={key}>{part.slice(1, -1)}</em>;
    return <Fragment key={key}>{part}</Fragment>;
  });
}

export function Markdown({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <>
      {blocks.map((block, bi) => {
        const lines = block.split("\n");
        if (lines.every((l) => /^\s*-\s+/.test(l))) {
          return (
            <ul key={bi}>
              {lines.map((l, li) => (
                <li key={li}>{renderInline(l.replace(/^\s*-\s+/, ""), `${bi}-${li}-`)}</li>
              ))}
            </ul>
          );
        }
        if (lines.every((l) => /^\s*\d+\.\s+/.test(l))) {
          return (
            <ol key={bi}>
              {lines.map((l, li) => (
                <li key={li}>{renderInline(l.replace(/^\s*\d+\.\s+/, ""), `${bi}-${li}-`)}</li>
              ))}
            </ol>
          );
        }
        // mixed block: a lead line followed by a list
        const firstList = lines.findIndex((l) => /^\s*-\s+/.test(l));
        if (firstList > 0 && lines.slice(firstList).every((l) => /^\s*-\s+/.test(l))) {
          return (
            <Fragment key={bi}>
              <p>{renderInline(lines.slice(0, firstList).join(" "), `${bi}-p-`)}</p>
              <ul>
                {lines.slice(firstList).map((l, li) => (
                  <li key={li}>{renderInline(l.replace(/^\s*-\s+/, ""), `${bi}-${li}-`)}</li>
                ))}
              </ul>
            </Fragment>
          );
        }
        return <p key={bi}>{renderInline(lines.join(" "), `${bi}-`)}</p>;
      })}
    </>
  );
}
