"use client";

import { useEffect, useRef } from "react";

export interface ShortcutHandlers {
  save: () => void;
  nextIssue: () => void;
  previousIssue: () => void;
  approve: () => void;
  nextDocument: () => void;
  help: () => void;
}

export const SHORTCUTS: ReadonlyArray<{ keys: string; action: string }> = [
  { keys: "Ctrl/⌘ + S", action: "Save changes" },
  { keys: "J / K", action: "Next / previous issue" },
  { keys: "A", action: "Approve" },
  { keys: "N", action: "Next document needing review" },
  { keys: "[ / ]", action: "Previous / next page (viewer focused)" },
  { keys: "Ctrl/⌘ + Enter", action: "Submit a note (resolve / reject dialogs)" },
  { keys: "?", action: "Show this help" },
];

function typingIn(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
  );
}

/**
 * Review shortcuts. Single-letter keys are ignored while typing in a field (so
 * "a" in a supplier name never approves) and when a dialog is open; Ctrl/Cmd+S
 * works everywhere and replaces the browser's "save page".
 */
export function useShortcuts(handlers: ShortcutHandlers) {
  const ref = useRef(handlers);
  useEffect(() => {
    ref.current = handlers;
  });

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const h = ref.current;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        h.save();
        return;
      }
      if (event.ctrlKey || event.metaKey || event.altKey || typingIn(event.target)) {
        return;
      }
      if (document.querySelector("[role=dialog]")) {
        return;
      }
      const action = { j: h.nextIssue, k: h.previousIssue, a: h.approve, n: h.nextDocument, "?": h.help }[
        event.key
      ];
      if (action) {
        event.preventDefault();
        action();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, []);
}
