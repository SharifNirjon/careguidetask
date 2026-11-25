import type { Segment } from "@/lib/api/types";

/**
 * Snippets arrive as structured segments and are rendered as TEXT nodes:
 * document content can't inject markup (no dangerouslySetInnerHTML anywhere).
 */
export function Snippet({ segments }: { segments: Segment[] }) {
  if (segments.length === 0) {
    return null;
  }
  return (
    <p className="text-muted-foreground text-sm">
      {segments.map((segment, i) =>
        segment.highlight ? <mark key={i}>{segment.text}</mark> : <span key={i}>{segment.text}</span>,
      )}
    </p>
  );
}
