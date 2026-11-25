"use client";

import { useCallback, useEffect } from "react";

const MESSAGE = "You have unsaved changes. Leave without saving?";

/**
 * Unsaved-changes guard. The App Router has no "before route change" event, so
 * this covers the three ways to leave: closing/reloading the tab (beforeunload),
 * clicking an in-app link (captured before Next handles it), and programmatic
 * navigation (callers ask `confirmLeave()` first).
 */
export function useUnsavedGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) {
      return;
    }
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    const onClick = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (
        !(anchor instanceof HTMLAnchorElement) ||
        anchor.target === "_blank" ||
        anchor.hasAttribute("download")
      ) {
        return;
      }
      if (new URL(anchor.href).origin === window.location.origin && !window.confirm(MESSAGE)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("click", onClick, true);
    };
  }, [dirty]);

  return useCallback(() => !dirty || window.confirm(MESSAGE), [dirty]);
}
