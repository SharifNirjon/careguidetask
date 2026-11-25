import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { UploadOutcome } from "@/lib/api/types";
import { document as documentFactory } from "@/test/factories";
import { renderWithClient } from "@/test/render";
import { API, server } from "@/test/server";

import { uploadFiles } from "./api";
import { UploadDialog } from "./components/upload-dialog";

const QUEUED_ID = "55555555-5555-4555-8555-555555555555";
const ORIGINAL_ID = "66666666-6666-4666-8666-666666666666";

const OUTCOMES: UploadOutcome[] = [
  {
    filename: "invoice.pdf",
    document_id: QUEUED_ID,
    status: "queued",
    duplicate_of_document_id: ORIGINAL_ID,
    error: null,
  },
  {
    filename: "virus.pdf",
    document_id: null,
    status: "rejected",
    duplicate_of_document_id: null,
    error: { code: "TYPE_MISMATCH", detail: "The file content does not match its extension." },
  },
];

function pdf(name: string): File {
  return new File(["%PDF-1.7 test"], name, { type: "application/pdf" });
}

describe("upload flow", () => {
  it("shows per-file outcomes, and a retry reuses the Idempotency-Key", async () => {
    const keys: (string | null)[] = [];
    let attempts = 0;
    server.use(
      http.post(`${API}/documents`, ({ request }) => {
        keys.push(request.headers.get("idempotency-key"));
        attempts += 1;
        return attempts === 1 ? HttpResponse.error() : HttpResponse.json(OUTCOMES, { status: 202 });
      }),
      http.get(`${API}/documents/${QUEUED_ID}`, () =>
        HttpResponse.json(
          documentFactory({
            id: QUEUED_ID,
            status: "processing",
            progress: { ...documentFactory().progress, stage: "parse", percent: 20 },
          }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderWithClient(<UploadDialog />);

    await user.click(screen.getByRole("button", { name: /upload/i }));
    await user.upload(screen.getByLabelText("Choose files to upload"), [
      pdf("invoice.pdf"),
      pdf("virus.pdf"),
    ]);
    await user.click(screen.getByRole("button", { name: "Upload 2 files" }));

    const failure = await screen.findByRole("alert");
    expect(within(failure).getByText("Upload failed")).toBeInTheDocument();
    await user.click(within(failure).getByRole("button", { name: /try again/i }));

    expect(await screen.findByText("1 of 2 files accepted.")).toBeInTheDocument();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toMatch(/^upload-/);
    expect(keys[1]).toBe(keys[0]); // same batch, same key: the retry can't create duplicates

    expect(screen.getByText("The file's content doesn't match its extension.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "an earlier upload" })).toHaveAttribute(
      "href",
      `/documents/${ORIGINAL_ID}`,
    );
    expect(await screen.findByText("Processing")).toBeInTheDocument(); // live status of the queued file
    expect(screen.getByText(/parse · 20%/)).toBeInTheDocument();
  });

  it("a new selection starts a new batch (new key)", async () => {
    const keys: (string | null)[] = [];
    server.use(
      http.post(`${API}/documents`, ({ request }) => {
        keys.push(request.headers.get("idempotency-key"));
        return HttpResponse.json([OUTCOMES[1]], { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderWithClient(<UploadDialog />);
    await user.click(screen.getByRole("button", { name: /upload/i }));
    await user.upload(screen.getByLabelText("Choose files to upload"), pdf("a.pdf"));
    await user.click(screen.getByRole("button", { name: "Upload 1 file" }));
    await user.click(await screen.findByRole("button", { name: "Upload more" }));
    await user.upload(screen.getByLabelText("Choose files to upload"), pdf("b.pdf"));
    await user.click(screen.getByRole("button", { name: "Upload 1 file" }));
    await waitFor(() => {
      expect(keys).toHaveLength(2);
    });
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("maps a 413 to a friendly error with the request id", async () => {
    server.use(
      http.post(`${API}/documents`, () =>
        HttpResponse.json(
          {
            code: "file_too_large",
            detail: "1 file(s) exceed the 100 MB limit.",
            request_id: "req-413",
            errors: [],
          },
          { status: 413 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderWithClient(<UploadDialog />);
    await user.click(screen.getByRole("button", { name: /upload/i }));
    await user.upload(screen.getByLabelText("Choose files to upload"), pdf("big.pdf"));
    await user.click(screen.getByRole("button", { name: "Upload 1 file" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("A file is larger than the upload limit.")).toBeInTheDocument();
    expect(within(alert).getByText("req-413")).toBeInTheDocument();
  });
});

describe("uploadFiles progress", () => {
  it("reports XHR upload progress", async () => {
    const progress: number[] = [];
    class FakeXhr {
      upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
      status = 202;
      response: unknown = [];
      responseType = "";
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      open() {}
      setRequestHeader() {}
      getResponseHeader() {
        return null;
      }
      abort() {}
      send() {
        for (const loaded of [25, 50, 100]) {
          this.upload.onprogress?.({ lengthComputable: true, loaded, total: 100 } as ProgressEvent);
        }
        this.onload?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    await uploadFiles([pdf("a.pdf")], "upload-key", (p) => progress.push(p.loaded / p.total));
    expect(progress).toEqual([0.25, 0.5, 1]);
    vi.unstubAllGlobals();
  });
});
