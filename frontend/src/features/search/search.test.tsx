import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Snippet } from "./components/snippet";
import { useCommittedQuery } from "./hooks";
import { autoSearchDelay, canSubmit } from "./schemas";

describe("search rules (semantic queries spend quota)", () => {
  it.each([
    ["keyword", "a", 300],
    ["keyword", "", null],
    ["hybrid", "ab", null],
    ["hybrid", "abc", 600],
    ["semantic", "   ab  ", null],
  ] as const)("%s %j -> auto after %s ms", (mode, q, delay) => {
    expect(autoSearchDelay(mode, q)).toBe(delay);
  });

  it("Enter never runs semantic/hybrid under 3 characters", () => {
    expect(canSubmit("hybrid", "ab")).toBe(false);
    expect(canSubmit("keyword", "ab")).toBe(true);
  });
});

describe("useCommittedQuery debounce", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keyword commits 300 ms after typing stops", () => {
    const { result, rerender } = renderHook(({ q }) => useCommittedQuery(q, "keyword", ""), {
      initialProps: { q: "a" },
    });
    rerender({ q: "ac" });
    act(() => {
      vi.advanceTimersByTime(299);
    });
    expect(result.current.committed).toBe("");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.committed).toBe("ac");
  });

  it("hybrid waits 600 ms idle, and Enter commits at once", () => {
    const { result, rerender } = renderHook(({ q }) => useCommittedQuery(q, "hybrid", ""), {
      initialProps: { q: "acme" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(result.current.committed).toBe("");
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(result.current.committed).toBe("acme");
    rerender({ q: "acme march" });
    act(() => {
      result.current.submit();
    });
    expect(result.current.committed).toBe("acme march");
  });

  it("hybrid never runs for fewer than 3 characters", () => {
    const { result } = renderHook(() => useCommittedQuery("ab", "hybrid", ""));
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    let submitted = true;
    act(() => {
      submitted = result.current.submit();
    });
    expect(submitted).toBe(false);
    expect(result.current.committed).toBe("");
  });
});

describe("Snippet", () => {
  it("renders segments as text: markup in documents stays inert", () => {
    const { container } = render(
      <Snippet
        segments={[
          { text: 'Note <script>alert("x")</script> ', highlight: false },
          { text: "<b>payment</b>", highlight: true },
        ]}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("<b>payment</b>").tagName).toBe("MARK");
    expect(container.textContent).toBe('Note <script>alert("x")</script> <b>payment</b>');
  });
});
