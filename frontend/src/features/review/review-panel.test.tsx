import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defined } from "@/test/at";

import type { ExtractionOut } from "@/lib/api/types";
import { document, DOCUMENT_ID, extraction, EXTRACTION_ID, issue } from "@/test/factories";
import { renderWithClient } from "@/test/render";
import { API, server } from "@/test/server";

import { ReviewPanel } from "./components/review-panel";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => `/documents/${DOCUMENT_ID}`,
  useSearchParams: () => new URLSearchParams(),
}));

function renderPanel(ex: ExtractionOut = extraction()) {
  server.use(
    http.get(`${API}/extractions/${EXTRACTION_ID}/versions`, () => HttpResponse.json([])),
    http.get(`${API}/extractions/${EXTRACTION_ID}/corrections`, () => HttpResponse.json([])),
    http.get(`${API}/documents/${DOCUMENT_ID}`, () => HttpResponse.json(document())),
    http.get(`${API}/documents/${DOCUMENT_ID}/results`, () =>
      HttpResponse.json({ document_id: DOCUMENT_ID, status: "needs_review", extractions: [ex] }),
    ),
    http.get(`${API}/documents`, () => HttpResponse.json({ items: [], next_cursor: null })),
  );
  return renderWithClient(
    <ReviewPanel
      document={document()}
      extraction={ex}
      onShowOnPage={vi.fn()}
      onRefetchResults={() => Promise.resolve()}
    />,
  );
}

describe("fields form", () => {
  beforeEach(() => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("PATCH sends only dirty fields with If-Match = lock_version", async () => {
    let captured: { body: unknown; ifMatch: string | null } | null = null;
    server.use(
      http.patch(`${API}/extractions/${EXTRACTION_ID}`, async ({ request }) => {
        captured = { body: await request.json(), ifMatch: request.headers.get("if-match") };
        return HttpResponse.json(extraction({ lock_version: 4, issues: [] }));
      }),
    );
    const user = userEvent.setup();
    renderPanel();
    const total = screen.getByLabelText(/^Total/);
    await user.clear(total);
    await user.type(total, "270.00");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      expect(captured).not.toBeNull();
    });
    expect(captured).toEqual({ body: { fields: { total: "270.00" } }, ifMatch: "3" });
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("Ctrl+S saves", async () => {
    const patch = vi.fn(() => HttpResponse.json(extraction({ lock_version: 4 })));
    server.use(http.patch(`${API}/extractions/${EXTRACTION_ID}`, patch));
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/^Supplier/), " Group");
    await user.keyboard("{Control>}s{/Control}");
    await waitFor(() => {
      expect(patch).toHaveBeenCalledTimes(1);
    });
  });

  it("412 opens the conflict dialog", async () => {
    server.use(
      http.patch(`${API}/extractions/${EXTRACTION_ID}`, () =>
        HttpResponse.json(
          { code: "version_mismatch", detail: "changed", request_id: "r", errors: [] },
          { status: 412 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/^Customer/), " Ltd");
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    const dialog = await screen.findByRole("dialog", { name: "This invoice changed while you were editing" });
    expect(within(dialog).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Keep my changes" })).toBeInTheDocument();
  });

  it("422 shows the server's errors inline", async () => {
    server.use(
      http.patch(`${API}/extractions/${EXTRACTION_ID}`, () =>
        HttpResponse.json(
          {
            code: "invalid_values",
            detail: "Some values could not be parsed.",
            request_id: "r",
            errors: [
              {
                loc: ["body", "fields", "invoice_date"],
                msg: "ambiguous day/month order",
                type: "invalid_invoice_date",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderPanel();
    const date = screen.getByLabelText(/^Invoice date/);
    await user.clear(date);
    await user.type(date, "2026-03-04");
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText("ambiguous day/month order")).toBeInTheDocument();
    expect(date).toHaveAttribute("aria-invalid", "true");
  });

  it("client-side validation blocks a malformed amount before any request", async () => {
    const patch = vi.fn(() => HttpResponse.json(extraction()));
    server.use(http.patch(`${API}/extractions/${EXTRACTION_ID}`, patch));
    const user = userEvent.setup();
    renderPanel();
    const total = screen.getByLabelText(/^Total/);
    await user.clear(total);
    await user.type(total, "12,50");
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText("Use a plain number, e.g. 1234.56")).toBeInTheDocument();
    expect(patch).not.toHaveBeenCalled();
  });
});

describe("issues and approval", () => {
  it("Apply suggestion fills the field (the user still saves)", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole("button", { name: /apply suggestion/i }));
    expect(screen.getByLabelText(/^Total/)).toHaveValue("270.00");
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  it("Approve is disabled with an explanation while errors are open", () => {
    renderPanel();
    const approve = screen.getByRole("button", { name: /approve/i });
    expect(approve).toBeDisabled();
    expect(approve).toHaveAccessibleDescription(
      "Approve is unavailable: 1 unresolved error: fix the values or resolve the issues first.",
    );
  });

  it("Approve is enabled once the error is resolved", () => {
    renderPanel(extraction({ issues: [issue({ resolved: true, resolution_note: "credit applied" })] }));
    expect(screen.getByRole("button", { name: /approve/i })).toBeEnabled();
  });

  it("shows ungrounded model suggestions without filling them in", () => {
    const ex = extraction();
    ex.fields.invoice_number = {
      ...defined(ex.fields.invoice_number),
      value: null,
      raw_value: "INV-9999",
      grounded: false,
    };
    renderPanel(ex);
    expect(screen.getByText(/not found in document/, { selector: "p" })).toHaveTextContent(
      "Model suggested INV-9999 — not found in document",
    );
    expect(screen.getByLabelText(/^Invoice number/)).toHaveValue("");
  });
});
