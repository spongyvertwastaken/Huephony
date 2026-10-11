import type { ReactNode } from "react";

// Renders the small Markdown subset used by the in-app legal content:
// headings, paragraphs, "- " lists, **bold**,
// `code` and [links](url). Links open through onOpenLink so they leave the app
// window for the browser.

type Block =
  | { kind: "heading"; level: 1 | 2 | 3; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; items: string[] };

function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: string[] | null = null;
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
    if (list) blocks.push({ kind: "list", items: list });
    paragraph = [];
    list = null;
  };
  for (const raw of source.split(/\r?\n/)) {
    const line = raw.trim();
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (!line) {
      flush();
    } else if (heading) {
      flush();
      blocks.push({ kind: "heading", level: heading[1].length as 1 | 2 | 3, text: heading[2] });
    } else if (line.startsWith("- ")) {
      if (paragraph.length) flush();
      list = list ?? [];
      list.push(line.slice(2));
    } else if (list) {
      // A wrapped line continues the current list item.
      list[list.length - 1] += ` ${line}`;
    } else {
      paragraph.push(line);
    }
  }
  flush();
  return blocks;
}

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g;

function renderInline(text: string, onOpenLink: (url: string) => void): ReactNode[] {
  return text.split(INLINE).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
    if (link) {
      const [, label, url] = link;
      return (
        <a
          key={index}
          href={url}
          onClick={(event) => {
            event.preventDefault();
            onOpenLink(url);
          }}
        >
          {label}
        </a>
      );
    }
    return part;
  });
}

export function Markdown({ source, onOpenLink }: { source: string; onOpenLink: (url: string) => void }) {
  return (
    <div className="st-doc">
      {parseBlocks(source).map((block, index) => {
        if (block.kind === "heading") {
          const Tag = block.level === 1 ? "h3" : block.level === 2 ? "h4" : "h5";
          return <Tag key={index}>{renderInline(block.text, onOpenLink)}</Tag>;
        }
        if (block.kind === "list") {
          return (
            <ul key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{renderInline(item, onOpenLink)}</li>
              ))}
            </ul>
          );
        }
        return <p key={index}>{renderInline(block.text, onOpenLink)}</p>;
      })}
    </div>
  );
}
